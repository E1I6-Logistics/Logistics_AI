"""[모듈: kev_engine.py - Kev 4단계 의사결정 MAPF 코어 엔진]
- 역할: 공유 구간의 로봇 쌍 충돌을 반복 조정하여 WAIT / REROUTE / HOLD 행동 제안과 우선순위 관계를 생성한다.
- 의사결정 4단계 파이프라인:
  1단계 (_select_zone): 가장 먼저 해결할 공유 충돌 구간(zone) 선택
  2단계 (_resolve_priority): 순환을 일으키지 않는 후보 중 우선 통과 로봇(high)과 양보 로봇(low) 결정 (score/choice 모드 지원)
  3단계 (_select_action): 양보 로봇의 행동(WAIT / REROUTE / HOLD) 결정
  4단계 (_apply_wait / _reroute): 상류 대기 노드 예약 및 경로 교차 검증, 또는 가상 그래프 기반 우회 경로 탐색
"""

from __future__ import annotations

import time
from typing import Any

from backend.app.services import route_service

from .kev_client import (
    KEV_ACTION_INSTRUCTIONS,
    KEV_PRIORITY_INSTRUCTIONS,
    KEV_WAIT_INSTRUCTIONS,
    KEV_ZONE_INSTRUCTIONS,
    LiveKEV,
)
from .kev_conflict import (
    first_zone_index,
    group_conflict_zones,
    would_cycle,
    zone_candidates,
)
from .kev_graph import Graph, _node


class KevMAPF:
    """Kev 의사결정 클라이언트를 사용해 다중 로봇 간 공유 구간 충돌을 반복적으로 조정하는 엔진."""

    def __init__(
        self,
        graph: Graph,
        robots: dict[str, dict],
        live: Any,
        config: dict,
        *,
        rounds: int = 16,
        reroutes: int = 3,
    ):
        self.graph, self.robots, self.live = graph, robots, live
        self.rounds, self.reroutes = rounds, reroutes

        self.priority_mode = config.get("priority_mode", "choice")
        if self.priority_mode not in ("choice", "score"):
            raise ValueError("Invalid priority_mode")

        self.priority_scores: dict[str, float] = {}
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
        self.wait_reservations: dict[int, str] = {}
        self.robot_wait_nodes: dict[str, int] = {}

    def _zone_occupants(self, zid: str, info: dict) -> list[str]:
        """그룹에 속한 자원들의 현재 점유 로봇을 조회한다."""
        resource_ids = [zid] + info.get("resources", [])
        return sorted({self.occupied[rid] for rid in resource_ids if rid in self.occupied})

    def _graph_state(self) -> dict:
        """Kev 질의의 `state`에 공통으로 포함할 compact 그래프와 각 로봇의 인접 간선 정보를 구성한다."""
        return {
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

    def _wait_candidates(self, robot: dict, info: dict) -> list[int]:
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
                or self.wait_reservations.get(n) not in (None, robot_id)
                or (assigned_node is not None and n != assigned_node)
            ):
                continue
            if n not in result:
                result.append(n)
        return result

    def _reroute(
        self, robot: dict, zid: str, info: dict
    ) -> tuple[list[int] | None, str | None]:
        """충돌 구간(`info`)과 차단 노드를 제외한 임시 가상 그래프에서 양보 로봇의 우회 경로를 탐색·검증한다."""
        forbid_nodes = set(self.blocked_nodes)
        forbid_edges: set[tuple[int, int]] = set()

        if info["kind"] == "node":
            forbid_nodes.add(info["node"])
        elif info["kind"] == "edge":
            a, b = info["edge"]
            forbid_edges.update({(a, b), (b, a)})
        elif info["kind"] == "group":
            forbid_nodes.update(info["nodes"])
            for a, b in info["edges"]:
                forbid_edges.update({(a, b), (b, a)})
        else:
            raise ValueError(f"Unknown zone kind: {info['kind']}")

        if robot["current_node"] in forbid_nodes or robot["goal_node"] in forbid_nodes:
            return (
                None,
                "reroute cannot exclude current/goal node; use HOLD or change a task",
            )

        virtual = self.graph.virtual_graph(forbid_nodes, forbid_edges)
        try:
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
            return candidate, None
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"

    def _select_zone(self, it: int, available_zones: dict[str, dict]) -> tuple[str, dict]:
        """[1단계] 해결할 공유 충돌 구간(zone)을 선택한다."""
        zchoices = {
            f"z{i}": (
                f"zone={zid}, "
                f"sharing={','.join(info['robots'])}, "
                f"occupant={','.join(self._zone_occupants(zid, info)) or 'FREE'}"
            )
            for i, (zid, info) in enumerate(available_zones.items())
        }
        zkey = self.live.pick(
            {**self._graph_state(), "zones": available_zones},
            zchoices,
            KEV_ZONE_INSTRUCTIONS,
            f"mapf_zone_{it}",
        )
        return list(available_zones.items())[int(zkey.removeprefix("z"))]

    def _resolve_priority(
        self,
        it: int,
        zid: str,
        info: dict,
        pair: tuple[str, str],
        occupant: str | None,
        potential_conflict_zones: dict[str, dict],
    ) -> tuple[str, str] | None:
        """[2단계] 우선순위 사이클을 만들지 않는 후보 중 먼저 통과할 로봇(high)과 양보 로봇(low)을 선택한다."""
        a, b = pair
        possible = [
            p for p in ((a, b), (b, a)) if not would_cycle(self.priorities, p[0], p[1])
        ]
        if occupant in pair:
            other = b if occupant == a else a
            possible = (
                []
                if would_cycle(self.priorities, occupant, other)
                else [(occupant, other)]
            )
        if not possible:
            self.decisions.append(
                {
                    "zone": zid,
                    "robots": [a, b],
                    "action": "HOLD",
                    "reason": "all priority choices create cycles",
                }
            )
            return None

        if self.priority_mode == "score":
            if self.reroutes == 0:
                feasible = []
                for hi, lo in possible:
                    if not self._wait_candidates(self.robots[lo], info):
                        continue
                    high_path = {str(n) for n in self.robots[hi]["path"]}
                    low_curr = str(self.robots[lo]["path"][0])
                    if low_curr not in high_path:
                        feasible.append((hi, lo))
                if not feasible:
                    self.decisions.append(
                        {
                            "zone": zid,
                            "robots": [a, b],
                            "action": "HOLD",
                            "reason": "NO_FEASIBLE_WAIT_OR_REROUTE",
                        }
                    )
                    return None
                possible = feasible

            if not self.priority_scores:
                common_state = {
                    **self._graph_state(),
                    "conflict_zones": potential_conflict_zones,
                    "current_occupancy": self.occupied,
                }
                involved = sorted(
                    {rid for z in potential_conflict_zones.values() for rid in z["robots"]}
                )
                self.priority_scores.update(self.live.score_many(common_state, involved))

            return min(possible, key=lambda p: (-self.priority_scores[p[0]], p[0]))

        pri_map = {f"p{i}": (hi, lo) for i, (hi, lo) in enumerate(possible)}
        pchoices = {k: f"{hi} receives priority over {lo}" for k, (hi, lo) in pri_map.items()}
        choice = self.live.pick(
            self._state(zid, info, [a, b]),
            pchoices,
            KEV_PRIORITY_INSTRUCTIONS,
            f"priority_{it}",
        )
        high = pri_map[choice][0]
        return high, (b if high == a else a)

    def _apply_wait(
        self,
        it: int,
        zid: str,
        info: dict,
        pair: tuple[str, str],
        low: str,
        wait_nodes: list[int],
        result: dict[str, Any],
        status: str,
    ) -> str:
        """[4단계 - WAIT] 상류 대기 노드를 선택하고 예약 및 경로 중첩 여부를 검증한다."""
        wait_map = {f"w{i}": n for i, n in enumerate(wait_nodes)}
        wc = {
            k: f"Wait at node {n} (on remaining route before {zid})"
            for k, n in wait_map.items()
        }
        select = self.live.pick(
            {
                **self._state(zid, info, list(pair)),
                "wait_robot": low,
                "wait_nodes": wait_nodes,
            },
            wc,
            KEV_WAIT_INSTRUCTIONS,
            f"wait_node_{it}",
        )
        selected = wait_map[select]
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

    def resolve(self) -> dict:
        """최대 `self.rounds` 동안 공유 구간 충돌을 탐색하고 Kev 4단계 질의로 조정 계획을 수립한다."""
        start = time.perf_counter()
        status = "PROPOSED"
        routes = {r: x["path"] for r, x in self.robots.items()}
        potential_conflict_zones = group_conflict_zones(zone_candidates(routes))

        for it in range(self.rounds):
            available_zones = {
                zid: info
                for zid, info in potential_conflict_zones.items()
                if self._pair_for(zid, info) is not None
            }
            if not available_zones:
                break

            # [1단계] 충돌 구간 선택
            zid, info = self._select_zone(it, available_zones)
            pair = self._pair_for(zid, info)
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
            pri_pair = self._resolve_priority(
                it, zid, info, pair, occupant, potential_conflict_zones
            )
            if pri_pair is None:
                status = "BLOCKED"
                if (
                    self.decisions
                    and self.decisions[-1].get("reason") == "NO_FEASIBLE_WAIT_OR_REROUTE"
                ):
                    break
                continue
            high, low = pri_pair

            # [3단계] 양보 로봇의 행동 선택
            waiter = self.robots[low]
            wait_nodes = self._wait_candidates(waiter, info)
            action_choices = {}
            if wait_nodes:
                action_choices["action_wait"] = (
                    "WAIT: Stop at a permitted upstream node and allow the priority robot to proceed."
                )
            if self.reroute_count < self.reroutes:
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
                "action": "HOLD",
                "wait_node": None,
                "needs_fms_approval": True,
                "priority_robot_action": "REQUEST_PROCEED_AFTER_FMS_PERMISSION",
            }

            # [4단계] 선택한 행동 적용
            if action == "action_wait":
                status = self._apply_wait(
                    it, zid, info, pair, low, wait_nodes, result, status
                )
            elif action == "action_reroute":
                self.reroute_count += 1
                new_route, error = self._reroute(waiter, zid, info)
                if new_route is not None:
                    waiter["path"] = new_route
                    waiter["source"] = (
                        "kev_rerouted"
                        if isinstance(self.live, LiveKEV)
                        else "mock_rerouted"
                    )
                    result.update(action="REROUTE", new_path=new_route)
                    routes = {r: x["path"] for r, x in self.robots.items()}
                    potential_conflict_zones = group_conflict_zones(zone_candidates(routes))
                else:
                    result.update(action="HOLD", reason="REROUTE_FAILED", details=error)
                    status = "BLOCKED"
            else:
                result.update(action="HOLD", reason="KEV_SELECTED_HOLD")
                status = "BLOCKED"

            if result["action"] in ("WAIT", "REROUTE"):
                if not would_cycle(self.priorities, high, low):
                    self.priorities.add((high, low))
                else:
                    result.update(action="HOLD", reason="PRIORITY_CYCLE_DETECTED")
                    status = "BLOCKED"

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

        return {
            "status": status,
            "robot_count": len(self.robots),
            "paths": {rid: r["path"] for rid, r in sorted(self.robots.items())},
            "path_sources": {rid: r["source"] for rid, r in sorted(self.robots.items())},
            "potential_shared_zones": remaining,
            "decisions": self.decisions,
            "global_priority_relations": [list(x) for x in sorted(self.priorities)],
            "time_prediction_used": False,
            "fms_execution_approved": False,
            "safety": {
                "note": "ADVISORY ONLY. FMS must verify robot footprint/clearance, conflict-zone geometry, stop feasibility, occupancy freshness, atomic zone permits and real zone-clear events.",
                "path_validation": "directed graph edges checked",
                "geometric_intersections": "not checked unless modeled as shared nodes/edges",
                "requires_independent_collision_interlock": True,
            },
            "metrics": {
                "kev_choice_calls": self.live.calls,
                "kev_forced_choices": self.live.forced,
                "kev_estimated_total_wall_seconds": round(self.live.model_seconds, 5),
                "reroutes": self.reroute_count,
                "script_wall_seconds": round(time.perf_counter() - start, 5),
            },
            "warnings": self.warnings + list(getattr(self.live, "warnings", [])),
        }
