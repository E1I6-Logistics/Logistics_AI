"""[모듈: kev_engine.py - Kev 4단계 의사결정 MAPF 코어 엔진]
- 역할: 전체 로봇 경로 및 공유 구간을 탐색하여, 충돌이 없으면 기존 경로(PROCEED)를 유지하고,
  충돌 발생 시 KEV 인공지능이 로봇 간 우선순위(PROCEED 후보 확정) 및 양보 로봇의 행동(WAIT/REROUTE/HOLD)을 결정한다.
- AX 의사결정 파이프라인:
  [전체 로봇 경로·상태 입력] -> [공유 Node/Edge 탐색]
    -> 충돌 없음: 기존 경로 유지 (PROCEED)
    -> 충돌 발생:
       1단계 (_select_zone): 충돌 구역 선정
       2단계 (_resolve_priority): KEV 점수 일괄 평가 (score_many) -> 고정된 선행·양보 관계
       3단계 (_select_action): KEV 양보(Low) 로봇 행동 선택 (WAIT / REROUTE / HOLD)
       4단계 (_apply_wait / _reroute): 상류 안전 대기 노드(wait_node) 배정 또는 KEV 우회 경로 생성
"""

from __future__ import annotations

import math
import time
from typing import Any

from backend.app.services import route_service

from .kev_client import (
    KEV_ACTION_INSTRUCTIONS,
    KevPlanner,
)
from .kev_conflict import (
    first_zone_index,
    group_conflict_zones,
    zone_candidates,
)
from .kev_graph import Graph, _node
from .kev_traffic import mapf_traffic


class KevMAPF:
    """Kev 의사결정 클라이언트를 사용해 다중 로봇 간 공유 구간 충돌을 반복적으로 조정하는 엔진."""

    def __init__(
        self,
        graph: Graph,
        robots: dict[str, dict],
        live: KevPlanner,
        config: dict,
        *,
        rounds: int = 16,
        reroutes: int = 3,
    ):
        self.graph, self.robots, self.live = graph, robots, live
        self.rounds, self.reroutes = rounds, reroutes

        self.priority_mode = config.get("priority_mode", "score")
        if self.priority_mode != "score":
            raise ValueError("MAPF priority_mode supports only score")

        self.priority_scores: dict[str, float] = {}
        self._priority_rank: dict[str, int] = {}
        self.priority_score_calls = 0
        self.allow_proactive_hold = bool(config.get("allow_proactive_hold", False))
        self.occupied: dict[str, str] = {
            str(k): str(v)
            for k, v in (config.get("zone_occupancy") or {}).items()
            if v is not None
        }
        provided = config.get("wait_allowed_nodes")
        self.has_wait_policy = provided is not None or bool(graph.declared_wait_nodes())
        self.certified_wait = (
            {_node(n) for n in provided} if provided is not None else graph.declared_wait_nodes()
        )
        for n in self.certified_wait:
            if n not in graph.nodes:
                raise ValueError(f"wait_allowed_nodes contains missing node {n}")

        self.blocked_nodes = {_node(n) for n in config.get("blocked_nodes", [])}
        self.priorities: set[tuple[str, str]] = set()
        self.decisions: list[dict] = []
        self.warnings: list[str] = []
        self.reroute_count = 0
        self.processed: set[tuple[str, str, str]] = set()
        self.reserved_wait_nodes = {
            _node(node): set(owners)
            for node, owners in (config.get("wait_node_reservations") or {}).items()
        }
        self.wait_reservations: dict[int, str] = {}
        self.robot_wait_nodes: dict[str, int] = {}
        self._cached_graph_state: dict | None = None

    def _zone_occupants(self, zid: str, info: dict) -> list[str]:
        """그룹에 속한 자원들의 현재 점유 로봇을 조회한다."""
        resource_ids = [zid] + info.get("resources", [])
        return sorted({self.occupied[rid] for rid in resource_ids if rid in self.occupied})

    def _graph_state(self) -> dict:
        """Kev 질의의 `state`에 공통으로 포함할 compact 그래프와 각 로봇의 인접 간선 정보를 구성한다."""
        if self._cached_graph_state is None:
            self._cached_graph_state = {
                "route_graph": self.graph.compact,
                "robots": [
                    {
                        **robot,
                        "available_edges": route_service.get_available_edges(
                            self.graph.compact, robot["current_node"]
                        ),
                    }
                    for robot in self.robots.values()
                ],
                "eta_available": False,
            }
        return self._cached_graph_state

    def _state(self, zone: str, info: dict, participants: list[str]) -> dict:
        """특정 충돌 구간(`zone`)과 참여 로봇(`participants`)에 대한 상세 의사결정 컨텍스트를 생성한다."""
        occupants = self._zone_occupants(zone, info)
        return {
            **self._graph_state(),
            "shared_resource": zone,
            "shared_resource_details": info,
            "occupant": occupants[0] if len(occupants) == 1 else None,
            "occupants": occupants,
            "participant_ids": participants,
            "known_priority_constraints": sorted([list(t) for t in self.priorities]),
            "safe_wait_nodes_verified": sorted(self.certified_wait),
            "eta_available": False,
        }

    def _pair_for(self, zone: str, info: dict) -> tuple[str, str] | None:
        """해당 공유 구간(`zone`)을 지나는 로봇들 중 아직 처리하지 않은 첫 번째 로봇 쌍 `(a, b)`를 반환한다."""
        rs = info["robots"]
        return next(
            (
                (a, b)
                for i, a in enumerate(rs)
                for b in rs[i + 1 :]
                if (zone, a, b) not in self.processed
            ),
            None,
        )

    def _wait_candidates(self, robot: dict, info: dict, priority_robot: str | None = None) -> list[int]:
        """충돌 구역 진입 전 대기 가능하며 현재 점유되지 않은 노드를 반환한다."""
        path = robot["path"]
        before = first_zone_index(path, info)
        if before is None:
            return []

        result: list[int] = []
        robot_id = robot["id"]
        other_current_nodes = {
            other["current_node"] for oid, other in self.robots.items() if oid != robot_id
        }
        assigned_node = self.robot_wait_nodes.get(robot_id)

        for n in path[:before]:
            if (
                n in self.blocked_nodes
                or (self.has_wait_policy and n not in self.certified_wait)
                or self.occupied.get(f"node:{n}") not in (None, robot_id)
                or n in other_current_nodes
                or bool(self.reserved_wait_nodes.get(n, set()) - {robot_id})
                or (priority_robot is not None and n in self.robots[priority_robot]["path"])
                or self.wait_reservations.get(n) not in (None, robot_id)
                or (assigned_node is not None and n != assigned_node)
            ):
                continue
            if n not in result:
                result.append(n)
        return result

    def _reroute_constraints(self, robot: dict, info: dict):
        """우회 가능성 검사와 KEV 경로 검증에 같은 제외 자원을 사용한다."""
        forbid_nodes = set(self.blocked_nodes)
        forbid_edges: set[tuple[int, int]] = set()

        curr = robot["current_node"]
        goal = robot["goal_node"]
        other_goals = {
            r["goal_node"]
            for rid, r in self.robots.items()
            if rid != robot["id"] and r.get("goal_node") is not None
        }
        forbid_nodes.update(other_goals - {curr, goal})

        if info["kind"] == "node":
            if info["node"] not in (curr, goal):
                forbid_nodes.add(info["node"])
            else:
                return (
                    None, None,
                    "reroute cannot exclude current/goal node; use HOLD or change a task",
                )
        elif info["kind"] == "edge":
            a, b = info["edge"]
            forbid_edges.update({(a, b), (b, a)})
        elif info["kind"] == "group":
            forbid_nodes.update(set(info["nodes"]) - {curr, goal})
            for a, b in info["edges"]:
                forbid_edges.update({(a, b), (b, a)})
        else:
            raise ValueError(f"Unknown zone kind: {info['kind']}")

        if curr in forbid_nodes or goal in forbid_nodes:
            return (
                None, None,
                "reroute cannot exclude current/goal node; use HOLD or change a task",
            )

        return forbid_nodes, forbid_edges, None

    def _can_reroute(self, robot: dict, info: dict) -> bool:
        if self.reroute_count >= self.reroutes or robot.get("occupied_edge"):
            return False
        nodes, edges, error = self._reroute_constraints(robot, info)
        if error:
            return False
        # Reachability only: Python never supplies a replacement route to KEV/FMS.
        seen, pending = {robot["current_node"]}, [robot["current_node"]]
        while pending:
            current = pending.pop()
            if current == robot["goal_node"]:
                return True
            for neighbor in self.graph.compact["adjacency"].get(str(current), []):
                target = neighbor["to"]
                if target not in seen and target not in nodes and (current, target) not in edges:
                    seen.add(target)
                    pending.append(target)
        return False

    def _reroute(self, robot: dict, zid: str, info: dict) -> tuple[list[int] | None, str | None]:
        """KEV가 생성한 우회 경로를 제외 자원과 원본 그래프에 대조한다."""
        forbid_nodes, forbid_edges, error = self._reroute_constraints(robot, info)
        if error:
            return None, error
        curr, goal = robot["current_node"], robot["goal_node"]
        virtual = self.graph.virtual_graph(forbid_nodes, forbid_edges)
        traffic = mapf_traffic(self.robots, robot["id"], occupied=self.occupied)
        try:
            try:
                candidate = self.live.route(
                    virtual,
                    robot["current_node"],
                    robot["goal_node"],
                    selector=robot["selector"],
                    model=robot["model"],
                    traffic=traffic,
                )
            except TypeError:
                candidate = self.live.route(
                    virtual,
                    robot["current_node"],
                    robot["goal_node"],
                    selector=robot["selector"],
                    model=robot["model"],
                )
            candidate = [_node(n) for n in candidate]
            self.graph.check_path(
                candidate, start=robot["current_node"], goal=robot["goal_node"]
            )
            if any(n in forbid_nodes for n in candidate):
                raise ValueError("generated path entered forbidden node")
            if any((a, b) in forbid_edges for a, b in zip(candidate, candidate[1:])):
                raise ValueError("generated path entered forbidden edge")
            if any(
                a in candidate and b in candidate
                for a, b in forbid_edges
                if a not in (curr, goal) and b not in (curr, goal)
            ):
                raise ValueError("generated path still traverses both endpoints of forbidden edge")
            return candidate, None
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"

    def _select_zone(self, available_zones: dict[str, dict]) -> tuple[str, dict]:
        """조정 순서만 결정한다. 진입 단계는 ETA나 통과 우선순위가 아니다."""
        current_resources = set()
        for robot in self.robots.values():
            if robot.get("occupied_edge") and len(robot["path"]) > 1:
                a, b = sorted(robot["path"][:2])
                current_resources.add(f"edge:{a}:{b}")
            elif not robot.get("occupied_edge"):
                current_resources.add(f"node:{robot['current_node']}")

        def key(item):
            zid, info = item
            occupied = bool(self._zone_occupants(zid, info)) or bool(
                current_resources.intersection([zid] + info.get("resources", []))
            )
            entries = [first_zone_index(self.robots[rid]["path"], info) for rid in info["robots"]]
            return (not occupied, min(entries), zid)

        return min(available_zones.items(), key=key)

    def _resolve_priority(
        self, pair: tuple[str, str], potential_conflict_zones: dict[str, dict],
    ) -> tuple[str, str]:
        """계획당 한 번 고정한 (-score, robot ID) 전체 순서로만 관계를 만든다."""
        if not self._priority_rank:
            common_state = {
                **self._graph_state(),
                "conflict_zones": potential_conflict_zones,
                "current_occupancy": self.occupied,
            }
            # A reroute may introduce a conflict with an initially unrelated robot.
            involved = sorted(self.robots)
            scores = self.live.score_many(common_state, involved)
            if not isinstance(scores, dict) or set(scores) != set(involved) or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) for value in scores.values()
            ):
                raise ValueError("KEV must return one finite priority score for every planned robot")
            self.priority_scores = dict(scores)
            self.priority_score_calls += 1
            ordered = sorted(involved, key=lambda rid: (-scores[rid], rid))
            self._priority_rank = {rid: rank for rank, rid in enumerate(ordered)}

        high, low = sorted(pair, key=self._priority_rank.__getitem__)
        return high, low

    def _apply_wait(
        self,
        low: str,
        wait_nodes: list[int],
        result: dict[str, Any],
        status: str,
    ) -> str:
        """[4단계 - WAIT] 상류 대기 노드를 선택하고 예약 및 경로 중첩 여부를 검증한다."""
        if not wait_nodes:
            result.update(action="HOLD", reason="NO_SAFE_WAIT_NODE")
            return "BLOCKED"
        path = self.robots[low]["path"]
        selected = min(wait_nodes, key=lambda node: (
            sum(node in other["path"] for rid, other in self.robots.items() if rid != low),
            -path.index(node), node,
        ))
        reserved_by = self.wait_reservations.get(selected)
        assigned_node = self.robot_wait_nodes.get(low)

        if (
            selected not in wait_nodes
            or (reserved_by is not None and reserved_by != low)
            or (assigned_node is not None and assigned_node != selected)
        ):
            result.update(action="HOLD", reason="WAIT_NODE_RESERVATION_CONFLICT")
            return "BLOCKED"

        self.wait_reservations[selected] = low
        self.robot_wait_nodes[low] = selected
        verified = selected in self.certified_wait
        result.update(
            action="WAIT",
            wait_node=selected,
            wait_node_verified=verified,
            note="FMS must reserve the wait node and stop robot before the conflict zone",
        )
        if not verified:
            if status == "PROPOSED":
                status = "REVIEW_REQUIRED"
            self.warnings.append(f"{low}: wait node {selected} is NOT physically certified")

        crossing_robots = sorted(
            rid
            for rid, other in self.robots.items()
            if rid != low and selected in other["path"]
        )
        result["wait_path_overlaps"] = crossing_robots
        if crossing_robots:
            self.warnings.append(
                f"{low}: WAIT node {selected} overlaps routes of {', '.join(crossing_robots)}; "
                "FMS traffic reservation verification required"
            )
            if status == "PROPOSED":
                status = "REVIEW_REQUIRED"
        return status

    def _is_vacating_pair(self, node: int, a: str, b: str) -> bool:
        """출발 노드를 비우는 경로인지 확인한다. 실제 점유 해제·진입 허가는 FMS가 검사한다."""
        rA, rB = self.robots.get(a), self.robots.get(b)
        if not rA or not rB:
            return False
        pathA, pathB = rA.get("path"), rB.get("path")
        if not pathA or not pathB:
            return False
        if pathA[0] == node and len(pathA) > 1 and pathB[0] != node:
            edgeA = (pathA[0], pathA[1])
            if len(pathB) > 1 and (pathB[0], pathB[1]) == (edgeA[1], edgeA[0]):
                return False
            return True
        if pathB[0] == node and len(pathB) > 1 and pathA[0] != node:
            edgeB = (pathB[0], pathB[1])
            if len(pathA) > 1 and (pathA[0], pathA[1]) == (edgeB[1], edgeB[0]):
                return False
            return True
        return False

    def resolve(self) -> dict:
        """최대 `self.rounds` 동안 공유 구간 충돌을 탐색하고 코드 선택과 KEV 판단으로 조정 계획을 수립한다."""
        start = time.perf_counter()
        status = "PROPOSED"
        routes = {r: x["path"] for r, x in self.robots.items()}
        potential_conflict_zones = group_conflict_zones(zone_candidates(routes))

        for it in range(self.rounds):
            zone_pairs: dict[str, tuple[str, str]] = {}
            available_zones: dict[str, dict] = {}
            for zid, info in potential_conflict_zones.items():
                pair = self._pair_for(zid, info)
                while pair is not None and info.get("kind") == "node" and self._is_vacating_pair(info["node"], *pair):
                    self.processed.add((zid, *pair))
                    pair = self._pair_for(zid, info)
                if pair is not None:
                    zone_pairs[zid] = pair
                    available_zones[zid] = info

            if not available_zones:
                break

            # [1단계] 충돌 구간 선택
            zid, info = self._select_zone(available_zones)
            pair = zone_pairs.get(zid) or self._pair_for(zid, info)
            if pair is None:
                continue

            a, b = pair
            self.processed.add((zid, a, b))
            occupants = self._zone_occupants(zid, info)

            if len(occupants) > 1:
                self.decisions.append(
                    {
                        "zone": zid,
                        "robots": [a, b],
                        "action": "HOLD",
                        "reason": f"multiple occupants: {occupants}",
                    }
                )
                status = "BLOCKED"
                continue

            occupant = occupants[0] if occupants else None
            if occupant and occupant not in (a, b):
                self.decisions.append(
                    {
                        "zone": zid,
                        "robots": [a, b],
                        "action": "HOLD",
                        "reason": f"occupied by {occupant}",
                    }
                )
                status = "BLOCKED"
                continue

            # [2단계] 우선순위 결정
            high, low = self._resolve_priority(pair, potential_conflict_zones)

            # [3단계] 점유는 순위를 바꾸지 않으며, 구역 안에서 상류 WAIT를 할 수 없다.
            waiter = self.robots[low]
            wait_nodes = [] if occupant == low or waiter.get("occupied_edge") else self._wait_candidates(waiter, info, high)
            action_choices = {}
            if wait_nodes:
                action_choices["action_wait"] = (
                    "WAIT: Stop at a permitted upstream node and allow the priority robot to proceed."
                )
            if self._can_reroute(waiter, info):
                action_choices["action_reroute"] = (
                    "REROUTE: Attempt to generate a valid alternative route around the shared zone."
                )
            if self.allow_proactive_hold or not action_choices:
                action_choices["action_hold"] = (
                    "HOLD: Do not dispatch this robot. Require a new planning or safety review."
                )

            action = self.live.pick(
                {
                    **self._state(zid, info, [a, b]),
                    "yield_robot": low,
                    "candidate_wait_nodes": wait_nodes,
                },
                action_choices,
                KEV_ACTION_INSTRUCTIONS,
                f"action_{it}",
            )

            result: dict[str, Any] = {
                "zone": zid,
                "robots": [a, b],
                "priority_robot": high,
                "yield_robot": low,
                "occupied_by": occupant,
                "shared_resource_details": info,
                "action": "HOLD",
                "wait_node": None,
                "needs_fms_approval": True,
                "priority_robot_action": "WAIT" if occupant == low else "PROCEED",
            }

            # [4단계] 선택한 행동 적용
            if action == "action_wait":
                status = self._apply_wait(
                    low, wait_nodes, result, status
                )
            elif action == "action_reroute":
                self.reroute_count += 1
                new_route, error = self._reroute(waiter, zid, info)
                if new_route is not None:
                    waiter["path"] = new_route
                    waiter["source"] = self.live.reroute_source
                    self._cached_graph_state = None
                    result.update(action="REROUTE", new_path=new_route)
                    routes = {r: x["path"] for r, x in self.robots.items()}
                    potential_conflict_zones = group_conflict_zones(zone_candidates(routes))
                    self.processed = {entry for entry in self.processed if low not in entry[1:]}
                else:
                    result.update(action="HOLD", reason="REROUTE_FAILED", details=error)
                    status = "BLOCKED"
            else:
                result.update(action="HOLD", reason="KEV_SELECTED_HOLD")
                status = "BLOCKED"

            if result["action"] == "WAIT":
                # Every edge increases the same frozen rank, so a cycle is impossible.
                self.priorities.add((high, low))
            elif result["action"] == "HOLD":
                result["priority_robot_action"] = "HOLD"

            self.decisions.append(result)
        else:
            status = "BLOCKED"
            self.warnings.append(f"max_rounds={self.rounds} reached; more conflicts may remain")

        remaining = group_conflict_zones(
            zone_candidates({r: x["path"] for r, x in self.robots.items()})
        )
        for rid, robot in self.robots.items():
            forbidden_used = sorted(set(robot["path"]) & self.blocked_nodes)
            if forbidden_used:
                status = "BLOCKED"
                self.warnings.append(
                    f"{rid}: path still contains explicitly blocked nodes {forbidden_used}"
                )
        if any(d["action"] == "HOLD" for d in self.decisions):
            status = "BLOCKED"

        # 구역 전체 HOLD에는 단일 양보 로봇이 없으므로 참여 로봇 모두 보류한다.
        yield_robots = {
            rid
            for d in self.decisions if d.get("action") in ("WAIT", "HOLD")
            for rid in ([d["yield_robot"]] if d.get("yield_robot") else d.get("robots", []))
        }
        yield_robots.update(
            d["priority_robot"] for d in self.decisions
            if d.get("priority_robot_action") in ("WAIT", "HOLD")
        )
        proceed_robots = sorted(set(self.robots) - yield_robots)

        return {
            "status": status,
            "robot_count": len(self.robots),
            "paths": {rid: r["path"] for rid, r in sorted(self.robots.items())},
            "path_sources": {rid: r["source"] for rid, r in sorted(self.robots.items())},
            "proceed_robots": proceed_robots,
            "potential_shared_zones": remaining,
            "decisions": self.decisions,
            "global_priority_relations": [list(x) for x in sorted(self.priorities)],
            "priority_mode": self.priority_mode,
            "priority_scores": dict(self.priority_scores),
            "time_prediction_used": False,
            "fms_execution_approved": False,
            "safety": {
                "note": "ADVISORY ONLY. FMS must verify robot footprint/clearance, conflict-zone geometry, stop feasibility, occupancy freshness, atomic zone permits and real zone-clear events.",
                "path_validation": "directed graph edges checked",
                "geometric_intersections": "not checked unless modeled as shared nodes/edges",
                "requires_independent_collision_interlock": True,
            },
            "metrics": {
                "kev_api_calls": self.live.calls,
                "kev_score_calls": self.priority_score_calls,
                "kev_choice_calls": self.live.calls - self.priority_score_calls,
                "kev_forced_choices": self.live.forced,
                "kev_estimated_total_wall_seconds": round(self.live.model_seconds, 5),
                "reroutes": self.reroute_count,
                "script_wall_seconds": round(time.perf_counter() - start, 5),
            },
            "warnings": self.warnings + list(self.live.warnings),
        }
