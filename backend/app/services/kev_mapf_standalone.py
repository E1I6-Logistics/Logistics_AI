from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


# 기존 FMS 그래프 구현을 재사용하면서 파일을 CLI로 직접 실행할 수 있도록 한다.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from backend.app.services import route_service

# 연결 위치: order_route_dispatcher.dispatch_order_route()의
# required_path = result["path"] 이후, navigate_to_node() 호출 전.
# 기존 result와 다른 로봇의 현재 상태·남은 경로를
# plan_mapf_from_route_results()에 전달한다.
#
# 반환값 required_paths[order["robot_id"]]는 FMS용 list[str] 경로 후보.
# WAIT/HOLD 처리와 실행 허가 후 기존 required_path 인자에 전달한다.
#
# 시뮬레이션: mock_fms.navigate_to_node()
#   → TrafficManager → DistanceAStar.plan_timed() → 예약·주행
# 실제 로봇: ros_gateway.navigate_to_node(_order_step=True)
#   → RealNavigation → TrafficManager → Nav2 NavigateThroughPoses
#
# 현재 자동 호출·상태 수집·주행 연결은 미적용이며 제안만 반환한다.

KEV_ZONE_INSTRUCTIONS = (
    "You are the MAPF conflict-zone selection component. The given candidates "
    "are physical shared node/edge resources found on multiple robot routes. "
    "Choose ONE candidate conflict to resolve first, preferably a nearby zone "
    "that blocks imminent progress. Do not invent IDs. Answer only with a "
    "supplied criteria ID. No travel time estimates are available."
)
KEV_PRIORITY_INSTRUCTIONS = (
    "You are the MAPF priority-decision component. Choose ONE robot to have "
    "priority at the indicated shared resource, considering current position, "
    "progress, task priority and occupancy. Avoid deadlocks and repeated "
    "unnecessary concessions. Return only the criteria ID."
)
KEV_ACTION_INSTRUCTIONS = (
    "You are the MAPF conflict-resolution component. Decide whether the "
    "lower-priority robot should wait before the shared resource or reroute "
    "around it. WAIT requires a safe upstream stopping location, and REROUTE "
    "must avoid the shared resource. Current resource occupants cannot be "
    "evicted. Return only a supplied criteria ID."
)
KEV_WAIT_INSTRUCTIONS = (
    "You are the MAPF waiting-position selection component. Choose one "
    "upstream node before the shared resource where the yielding robot should "
    "wait. Consider path continuity and whether another robot would be "
    "blocked. Physical clearance is verified separately by FMS. Return "
    "only a supplied criteria ID."
)


def _node(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        raise ValueError("node ID must be an integer or integer string (not bool/float)")
    return int(v)


def _node_sort(s: str) -> tuple[int, int | str]:
    try:
        return 0, int(s)
    except ValueError:
        return 1, s


class Graph:
    """기존 경로 서비스를 재사용하는 읽기 전용 유향 GeoJSON 그래프 정보."""
    def __init__(self, data: dict[str, Any]):
        if data.get("type") != "FeatureCollection":
            raise ValueError("--graph must be GeoJSON FeatureCollection")
        self.data = data
        # AI preview와 같은 저장 weight 규칙을 사용한다. 좌표 거리로 대체하지 않는다.
        self.nodes, self.edge_records = route_service.build_route_inputs(
            data, require_stored_weight=True
        )
        self.node_props = {
            _node(key): feature["properties"]
            for key, feature in route_service.node_lookup(data).items()
        }
        self.edges = route_service.build_edge_weight_lookup(self.nodes, self.edge_records)
        self.compact = route_service.build_compact_adjacency_graph(
            data, "kev-mapf-in-memory", require_stored_weight=True
        )

    @classmethod
    def load(cls, path: Path) -> Graph:
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def check_path(self, path: list[int], *, start: int | None = None, goal: int | None = None) -> None:
        normalized = [_node(n) for n in path]
        if not normalized:
            raise ValueError("path must be nonempty")
        route_service.validate_and_calculate_path_distance(
            self.nodes, self.edge_records, normalized,
            normalized[0] if start is None else start,
            normalized[-1] if goal is None else goal,
        )
        # 기존 예약 경계와 동일하게 유향 간선 재사용·노드 3회 이상 방문을 거부한다.
        steps = list(zip(normalized, normalized[1:]))
        if len(set(steps)) != len(steps) or any(n > 2 for n in Counter(normalized).values()):
            raise ValueError("path violates directed-edge reuse or node visit limit")

    def virtual_graph(self, forbidden_nodes: set[int], forbidden_edges: set[tuple[int, int]]) -> dict[str, Any]:
        """제외 구간을 제거한 임시 GeoJSON을 메모리에 만들고 원본은 보존한다."""
        features = []
        for f in self.data.get("features", []):
            p = f.get("properties") or {}
            if (f.get("geometry") or {}).get("type") == "Point":
                if p.get("id") is not None and _node(p["id"]) in forbidden_nodes:
                    continue
            elif p.get("startid") is not None and p.get("endid") is not None:
                a, b = _node(p["startid"]), _node(p["endid"])
                if a in forbidden_nodes or b in forbidden_nodes or (a, b) in forbidden_edges:
                    continue
            features.append(f)
        return {"type": "FeatureCollection", "features": features}

    def declared_wait_nodes(self) -> set[int]:
        return {
            n for n, p in self.node_props.items()
            if p.get("wait_allowed") is True or p.get("safe_wait") is True
        }


class LiveKEV:
    """기존 Logistics_AI의 경로 preview와 KevSelector를 재사용한다."""

    def __init__(self, repo: Path, *, model: str, deadline: float, max_steps: int | None):
        repo = repo.expanduser().resolve()
        target = repo / "backend/app/services/llm_route_preview.py"
        if not target.is_file():
            raise FileNotFoundError(f"Not a Logistics_AI checkout: {target}")
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
        # FMS 프로젝트에서 사용하는 Python 의존성이 필요하다.
        from backend.app.services.route_selector.kev_selector import KevSelector
        from backend.app.services.llm_route_preview import preview_llm_route
        self.selector = KevSelector(model=model)
        self.model = model
        self.preview_llm_route = preview_llm_route
        # deadline/max_steps는 CLI 호환용으로만 유지한다. 실제 시간·단계 제한은
        # preview가 기존 모델 카탈로그에서 읽는다.
        self.calls = 0
        self.model_seconds = 0.0
        self.forced = 0
        self.warnings: list[str] = []

    def pick(self, state: dict[str, Any], candidates: dict[str, str], instructions: str, question_id: str) -> str:
        if not candidates:
            raise ValueError(f"no selectable choices for {question_id}")
        if len(candidates) == 1:
            self.forced += 1
            return next(iter(candidates))
        res = self.selector.select_choice(state, candidates, instructions, question_id=question_id)
        self.calls += 1
        self.model_seconds += float(res.get("total_duration_seconds") or 0.0)
        choice = str(res["choice"])
        if choice not in candidates:
            raise RuntimeError(f"KEV returned an invalid choice {choice!r}")
        return choice

    def route(self, graph_data: dict, start: int, goal: int, *, selector: str = "kev", model: str | None = None) -> list[int]:
        """기존 FMS preview의 카탈로그·검증을 사용하며 오류는 호출자에게 전달한다."""
        started = time.perf_counter()
        try:
            # 새 경로/우회만 추론한다. 카탈로그 오류를 별도 runner로 우회하지 않는다.
            result = self.preview_llm_route(
                graph_data, str(start), str(goal), selector=selector,
                model=self.model if model is None else model,
            )
            if not isinstance(result, dict) or not isinstance(result.get("path"), list):
                raise ValueError("preview_llm_route() must return a dict with a path list")
            path = [_node(n) for n in result["path"]]
            Graph(graph_data).check_path(path, start=start, goal=goal)
            self.calls += int(result.get("calls", 0))
            return path
        finally:
            self.model_seconds += time.perf_counter() - started


class MockKEV:
    """오프라인 데모 전용이며 결과를 실제 모델 추론으로 취급하지 않는다."""
    calls = 0
    model_seconds = 0.0
    forced = 0

    def pick(self, state, candidates, instructions, question_id):
        if len(candidates) == 1:
            self.forced += 1
        else:
            self.calls += 1
        # 대기 가능한 장소가 지정되었으면 대기를 우선 선택한다.
        if "action_wait" in candidates:
            return "action_wait"
        return next(iter(candidates))

    def route(self, graph_data, start, goal, *, selector="kev", model=None):
        # 데모·테스트 전용 BFS이다. 실제 모드에서는 기존 AI 경로 생성기를 사용한다.
        graph = Graph(graph_data)
        todo = [[start]]
        seen = {start}
        while todo:
            p = todo.pop(0)
            if p[-1] == goal:
                return p
            for a, b in sorted(graph.edges):
                if a == p[-1] and b not in seen:
                    seen.add(b)
                    todo.append(p + [b])
        raise RuntimeError("demo route not found")


def zone_candidates(paths: dict[str, list[int]]) -> dict[str, dict[str, Any]]:
    """여러 로봇이 공유할 가능성이 있는 노드와 양방향 물리 간선을 찾는다.

    동시 도착 시간은 예측하지 않는다. 공유 노드·간선으로 표현되지 않은
    기하학적 교차를 운영에 반영하려면 보정된 물리 구간 지도가 필요하다.
    """
    nodes_by: dict[int, set[str]] = defaultdict(set)
    edges_by: dict[tuple[int, int], set[str]] = defaultdict(set)
    for rid, path in paths.items():
        for n in path:
            nodes_by[n].add(rid)
        for a, b in zip(path, path[1:]):
            edges_by[tuple(sorted((a, b)))].add(rid)
    c: dict[str, dict[str, Any]] = {}
    for node, robots in nodes_by.items():
        if len(robots) >= 2:
            c[f"node:{node}"] = {"kind": "node", "node": node, "robots": sorted(robots)}
    for edge, robots in edges_by.items():
        if len(robots) >= 2:
            a, b = edge
            c[f"edge:{a}:{b}"] = {"kind": "edge", "edge": [a, b], "robots": sorted(robots)}
    return dict(sorted(c.items(), key=lambda x: x[0]))


def first_zone_index(path: list[int], zone: dict[str, Any]) -> int | None:
    if zone["kind"] == "node":
        return next((i for i, n in enumerate(path) if n == zone["node"]), None)
    a, b = zone["edge"]
    return next(
        (i + 1 for i, (u, v) in enumerate(zip(path, path[1:]))
         if {u, v} == {a, b}), None
    )


def would_cycle(priorities: set[tuple[str, str]], high: str, low: str) -> bool:
    if high == low:
        return True
    outgoing: dict[str, set[str]] = defaultdict(set)
    for a, b in priorities:
        outgoing[a].add(b)
    outgoing[high].add(low)
    stack, seen = [low], set()
    while stack:
        u = stack.pop()
        if u == high:
            return True
        if u in seen:
            continue
        seen.add(u)
        stack.extend(outgoing.get(u, []))
    return False


def mapf_robot_from_route_result(
    robot_id: str,
    current_node: int | str,
    goal_node: int | str,
    result: dict[str, Any],
    *,
    status: str = "IDLE",
    task_priority: int = 0,
) -> dict[str, Any]:
    """dispatcher의 실제 result['path']를 한 로봇의 MAPF 입력으로 변환한다.

    사용 예: mapf_robot_from_route_result(order['robot_id'], current_node,
                                          node, result)
    """
    if not isinstance(result, dict) or not isinstance(result.get("path"), list):
        raise ValueError("result must contain preview_llm_route() path list")
    # 경로를 복사해 str ID를 int로 정규화한다. result 원본을 수정하거나 재추론하지 않는다.
    return {
        "id": str(robot_id),
        "current_node": _node(current_node),
        "goal_node": _node(goal_node),
        "path": [_node(n) for n in result["path"]],
        "status": status,
        "task_priority": int(task_priority),
    }


def parse_robot_state(data: dict, graph: Graph, live: Any) -> dict[str, dict]:
    robots = {}
    for raw in data.get("robots", []):
        rid = str(raw["id"])
        if rid in robots:
            raise ValueError(f"duplicate robot id: {rid}")
        current, goal = _node(raw["current_node"]), _node(raw["goal_node"])
        if current not in graph.nodes or goal not in graph.nodes:
            raise ValueError(f"unknown start/goal of {rid}")
        # path, route_result, result 중 한 가지를 받으며, 모두 없으면 새 경로를 생성한다.
        # route_result에는 preview_llm_route()의 결과를 그대로 전달할 수 있다.
        candidates = [key for key in ("path", "route_result", "result") if raw.get(key) is not None]
        if len(candidates) > 1:
            raise ValueError(f"{rid}: choose one of path, route_result or result (not multiple)")
        if candidates:
            source = candidates[0]
            raw_path = raw["path"] if source == "path" else raw[source].get("path") if isinstance(raw[source], dict) else None
            if not isinstance(raw_path, list) or not raw_path:
                raise ValueError(f"{rid}: {source} must contain a nonempty path list")
            path = [_node(n) for n in raw_path]
            path_source = "provided" if source == "path" else "preview_result"
        else:
            path_source = "kev_preview" if not isinstance(live, MockKEV) else "mock_bfs"
            path = live.route(graph.data, current, goal,
                              selector=raw.get("selector", "kev"), model=raw.get("model"))
        if not path or path[0] != current or path[-1] != goal:
            raise ValueError(f"{rid}: path must begin at current_node and end at goal_node")
        graph.check_path(path, start=current, goal=goal)
        robots[rid] = {
            "id": rid,
            "current_node": current,
            "goal_node": goal,
            "path": path,
            "source": path_source,
            "status": str(raw.get("status", "IDLE")),
            "task_priority": int(raw.get("task_priority", 0)),
            "selector": raw.get("selector", "kev"),
            "model": raw.get("model"),
        }
    if not robots:
        raise ValueError("MAPF needs at least one robot")
    return robots


class KevMAPF:
    def __init__(self, graph: Graph, robots: dict[str, dict], live: Any, config: dict, *, rounds: int = 16, reroutes: int = 3):
        self.graph, self.robots, self.live = graph, robots, live
        self.rounds, self.reroutes = rounds, reroutes
        self.occupied: dict[str, str] = {
            str(k): str(v) for k, v in (config.get("zone_occupancy") or {}).items()
            if v is not None
        }
        provided = config.get("wait_allowed_nodes")
        self.has_wait_policy = provided is not None or bool(graph.declared_wait_nodes())
        self.certified_wait = ({_node(n) for n in provided} if provided is not None else graph.declared_wait_nodes())
        for n in self.certified_wait:
            if n not in graph.nodes:
                raise ValueError(f"wait_allowed_nodes contains missing node {n}")
        self.blocked_nodes = {_node(n) for n in config.get("blocked_nodes", [])}
        self.priorities: set[tuple[str, str]] = set()
        self.decisions: list[dict] = []
        self.warnings: list[str] = []
        self.reroute_count = 0
        self.processed: set[tuple[str, str, str]] = set()

    def _graph_state(self) -> dict:
        return {
            "route_graph": self.graph.compact,
            "robots": [
                {**robot, "available_edges": route_service.get_available_edges(
                    self.graph.compact, robot["current_node"]
                )}
                for robot in self.robots.values()
            ],
            "eta_available": False,
        }

    def _state(self, zone: str, info: dict, participants: list[str]) -> dict:
        return {
            **self._graph_state(),
            "shared_resource": zone,
            "shared_resource_details": info,
            "occupant": self.occupied.get(zone),
            "participant_ids": participants,
            "known_priority_constraints": sorted([list(t) for t in self.priorities]),
            "safe_wait_nodes_verified": sorted(self.certified_wait),
            "eta_available": False,
        }

    def _pair_for(self, zone: str, info: dict) -> tuple[str, str] | None:
        rs = info["robots"]
        pairs = [(a, b) for i, a in enumerate(rs) for b in rs[i+1:]]
        return next(((a, b) for a, b in pairs if (zone, a, b) not in self.processed), None)

    def _wait_candidates(self, robot: dict, info: dict) -> list[int]:
        path = robot["path"]
        before = first_zone_index(path, info)
        if before is None:
            return []
        # 로봇의 남은 경로에서 공유 구간에 진입하기 전의 노드만 대기 후보로 사용한다.
        result = []
        for n in path[:before]:
            if n in self.blocked_nodes:
                continue
            if self.has_wait_policy and n not in self.certified_wait:
                continue
            if n not in result:
                result.append(n)
        return result

    def _reroute(self, robot: dict, zid: str, info: dict) -> tuple[list[int] | None, str | None]:
        forbid_nodes = set(self.blocked_nodes)
        forbid_edges: set[tuple[int, int]] = set()
        if info["kind"] == "node":
            forbid_nodes.add(info["node"])
        else:
            a, b = info["edge"]
            forbid_edges.update({(a, b), (b, a)})
        if robot["current_node"] in forbid_nodes or robot["goal_node"] in forbid_nodes:
            return None, "reroute cannot exclude current/goal node; use HOLD or change a task"
        # 원본 지도는 보존하고 제외 구간을 제거한 임시 지도에서 주문의 AI 설정으로 우회한다.
        virtual = self.graph.virtual_graph(forbid_nodes, forbid_edges)
        try:
            candidate = self.live.route(
                virtual, robot["current_node"], robot["goal_node"],
                selector=robot["selector"], model=robot["model"],
            )
            candidate = [_node(n) for n in candidate]
            self.graph.check_path(candidate, start=robot["current_node"], goal=robot["goal_node"])
            for n in candidate:
                if n in forbid_nodes:
                    raise ValueError("generated path entered forbidden node")
            for a, b in zip(candidate, candidate[1:]):
                if (a, b) in forbid_edges:
                    raise ValueError("generated path entered forbidden edge")
            return candidate, None
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"

    def resolve(self) -> dict:
        start = time.perf_counter()
        status = "PROPOSED"
        for it in range(self.rounds):
            routes = {r: x["path"] for r, x in self.robots.items()}
            allzones = zone_candidates(routes)
            available_zones = {
                zid: info for zid, info in allzones.items()
                if self._pair_for(zid, info) is not None
            }
            if not available_zones:
                break
            zchoices = {
                f"z{i}": f"zone={zid}, sharing={','.join(info['robots'])}, "
                         f"occupant={self.occupied.get(zid, 'FREE')}"
                for i, (zid, info) in enumerate(available_zones.items())
            }
            zkey = self.live.pick(
                {**self._graph_state(), "zones": available_zones},
                zchoices, KEV_ZONE_INSTRUCTIONS, f"mapf_zone_{it}"
            )
            zid, info = list(available_zones.items())[int(zkey.removeprefix("z"))]
            pair = self._pair_for(zid, info)
            if pair is None:
                continue
            a, b = pair
            self.processed.add((zid, a, b))
            occupant = self.occupied.get(zid)
            if occupant and occupant not in (a, b):
                self.decisions.append({"zone": zid, "robots": [a, b], "action": "HOLD", "reason": f"occupied by {occupant}"})
                status = "BLOCKED"
                continue
            # 후보 ID는 짧고 안전한 문자열을 사용하며 로봇 이름의 문자 구성에 의존하지 않는다.
            possible = []
            if not would_cycle(self.priorities, a, b):
                possible.append((a, b))
            if not would_cycle(self.priorities, b, a):
                possible.append((b, a))
            if occupant in pair:
                possible = [(occupant, b if occupant == a else a)]
            if not possible:
                self.decisions.append({"zone": zid, "robots": [a, b], "action": "HOLD", "reason": "all priority choices create cycles"})
                status = "BLOCKED"
                continue
            pri_map = {f"p{i}": (hi, lo) for i, (hi, lo) in enumerate(possible)}
            pchoices = {key: f"{hi} receives priority over {lo}" for key, (hi, lo) in pri_map.items()}
            priority_choice = self.live.pick(self._state(zid, info, [a, b]), pchoices, KEV_PRIORITY_INSTRUCTIONS, f"priority_{it}")
            high = pri_map[priority_choice][0]
            low = b if high == a else a
            if not occupant:
                self.priorities.add((high, low))
            waiter = self.robots[low]
            wait_nodes = self._wait_candidates(waiter, info)
            action_choices = {}
            if wait_nodes:
                action_choices["action_wait"] = "WAIT at an upstream node, release only on real zone-clear event"
            if self.reroute_count < self.reroutes:
                action_choices["action_reroute"] = "REROUTE via the existing FMS preview with the robot route selector/model"
            action_choices["action_hold"] = "HOLD and require operator or future event (safe fallback)"
            action = self.live.pick(
                {**self._state(zid, info, [a, b]), "yield_robot": low,
                 "candidate_wait_nodes": wait_nodes},
                action_choices, KEV_ACTION_INSTRUCTIONS, f"action_{it}"
            )
            result: dict[str, Any] = {
                "zone": zid, "robots": [a, b], "priority_robot": high,
                "yield_robot": low, "occupied_by": occupant,
                "action": "HOLD", "wait_node": None,
                "needs_fms_approval": True,
                "priority_robot_action": "REQUEST_PROCEED_AFTER_FMS_PERMISSION",
            }
            if action == "action_wait":
                wait_map = {f"w{i}": n for i, n in enumerate(wait_nodes)}
                wc = {key: f"Wait at node {n} (on remaining route before {zid})" for key, n in wait_map.items()}
                select = self.live.pick(
                    {**self._state(zid, info, [a, b]), "wait_robot": low,
                     "wait_nodes": wait_nodes}, wc, KEV_WAIT_INSTRUCTIONS, f"wait_node_{it}"
                )
                selected = wait_map[select]
                result.update(action="WAIT", wait_node=selected,
                              wait_node_verified=(selected in self.certified_wait),
                              note="FMS must stop robot before zone and wait for actual release event")
                if not result["wait_node_verified"]:
                    status = "REVIEW_REQUIRED" if status == "PROPOSED" else status
                    self.warnings.append(f"{low}: wait node {selected} is NOT physically certified")
            elif action == "action_reroute":
                self.reroute_count += 1
                new_route, error = self._reroute(waiter, zid, info)
                if new_route is not None:
                    waiter["path"] = new_route
                    waiter["source"] = "kev_rerouted" if not isinstance(self.live, MockKEV) else "mock_rerouted"
                    result.update(action="REROUTE", new_path=new_route)
                else:
                    result.update(action="HOLD", reason="REROUTE_FAILED", details=error)
                    status = "BLOCKED"
            else:
                result.update(action="HOLD", reason="KEV_SELECTED_HOLD")
                status = "BLOCKED"
            self.decisions.append(result)
        else:
            status = "BLOCKED"
            self.warnings.append(f"max_rounds={self.rounds} reached; more conflicts may remain")

        remaining = zone_candidates({r: x["path"] for r, x in self.robots.items()})
        for rid, robot in self.robots.items():
            forbidden_used = sorted(set(robot["path"]) & self.blocked_nodes)
            if forbidden_used:
                status = "BLOCKED"
                self.warnings.append(f"{rid}: path still contains explicitly blocked nodes {forbidden_used}")
        if any(d["action"] == "HOLD" for d in self.decisions):
            status = "BLOCKED"
        # 공유 구간을 순차적으로 통과하도록 제어하면 경로 공유가 곧 충돌을 뜻하지 않는다.
        # WAIT는 실제 진입·해제 제어가 필요한 제안이며,
        # 충돌 없는 동시 주행 시간 계획을 보장하지 않는다.
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


def preview_order_route_for_mapf(graph: dict, order: dict, current_node: int | str, node: int | str) -> dict:
    """결과가 아직 없을 때만 dispatcher와 같은 preview 호출로 경로를 생성한다."""
    from backend.app.services.llm_route_preview import preview_llm_route

    return preview_llm_route(
        graph, current_node, node, selector=order["selector"], model=order["model"]
    )


def plan_mapf_from_route_results(
    graph: dict | Graph,
    route_requests: list[dict],
    *,
    planner: Any | None = None,
    config: dict | None = None,
) -> dict:
    """result['path'] 생성 이후, 실제 주행 요청 전에 사용하는 동기 계획 함수.

    각 요청은 order/current_node/node/result와 선택적인 status/task_priority를 받는다.
    호출자는 다른 로봇의 남은 경로도 result['path']로 전달하고,
    서로 일관된 최신 상태를 제공해야 한다. 정차 로봇은 current_node == node와
    한 노드 경로로 표현할 수 있다. 간선 점유는 zone_occupancy로 명시하며
    현재 노드를 임의로 추정하지 않는다. FMS 전역 객체 조회·주행·예약은 수행하지 않는다.

    planner는 LiveKEV 또는 테스트 대역처럼 pick(),
    route(..., selector=..., model=...)와 calls/forced/model_seconds를 제공한다.
    모든 result는 필수이며 제공된 경로를 복사해 사용하고 다시 추론하지 않는다.
    paths는 내부용 int ID, required_paths는 기존 주행기에 전달할 str ID 형식이다.
    WAIT/HOLD는 제안이므로 반환 경로를 자동으로 실행하지 않는다.
    """
    # 연결 예정: dispatcher의 required_path = result["path"] 이후, navigate_to_node() 이전.
    # 다른 로봇의 최신 상태·남은 경로도 호출자가 전달한다. 자동 수집은 미연결이다.
    # async에서는 asyncio.to_thread()로 호출하고 완료 후 기존 상태 유효성을 다시 검사한다.
    route_graph = graph if isinstance(graph, Graph) else Graph(graph)
    settings = dict(config or {})
    rounds = int(settings.get("max_rounds", 16))
    reroutes = int(settings.get("max_reroutes", 3))
    if rounds < 1 or reroutes < 0:
        raise ValueError("invalid planning limits")
    inputs = []
    for request in route_requests:
        order = request["order"]
        robot = mapf_robot_from_route_result(
            order["robot_id"], request["current_node"], request["node"], request["result"],
            status=request.get("status", "IDLE"),
            task_priority=request.get("task_priority", 0),
        )
        robot.update(selector=order["selector"], model=order["model"])
        if not robot["selector"] or not robot["model"]:
            raise ValueError("each order must specify its route selector and model")
        inputs.append(robot)
    # 실제 판단 클라이언트를 만들기 전에 제공된 모든 경로를 검증한다.
    robots = parse_robot_state({"robots": inputs}, route_graph, planner)
    for robot in robots.values():
        robot["source"] = "preview_result"
    if planner is None:
        planner = LiveKEV(
            Path(__file__).resolve().parents[3],
            model=settings.get("mapf_model", "kev-08b"),
            deadline=0.15, max_steps=None,
        )
    result = KevMAPF(route_graph, robots, planner, settings, rounds=rounds, reroutes=reroutes).resolve()
    # FMS 전달용 str ID로 변환한다. WAIT/HOLD 실행 정책과 허가를 연결하기 전에는
    # mock_fms/ros_gateway.navigate_to_node(required_path=...)로 자동 전달하지 않는다.
    result["required_paths"] = {
        rid: [str(n) for n in path] for rid, path in result["paths"].items()
    }
    return result


def demo_data() -> tuple[Graph, dict]:
    points = {1:(0,1),2:(1,1),3:(2,1),4:(3,1),5:(0,0),6:(1,0),7:(3,0),8:(4,1),9:(5,1)}
    edges = [(1,2),(2,3),(3,4),(5,6),(6,3),(3,7),(4,8),(8,9),(3,2),(2,1)]
    features = [
        {"type":"Feature", "geometry":{"type":"Point","coordinates":list(xy)},
         "properties":{"id":n,"wait_allowed":n in (2,6)}}
        for n,xy in points.items()
    ]
    features += [
        {"type":"Feature", "geometry":{"type":"LineString","coordinates":[list(points[a]),list(points[b])]},
         "properties":{"id":i+50,"startid":a,"endid":b,"weight":1.0}}
        for i,(a,b) in enumerate(edges)
    ]
    graph = Graph({"type":"FeatureCollection","features":features})
    jobs = {"robots":[
        {"id":"amr_1","current_node":1,"goal_node":4,"path":[1,2,3,4],"task_priority":1},
        {"id":"amr_2","current_node":5,"goal_node":7,"path":[5,6,3,7],"task_priority":2},
        {"id":"amr_3","current_node":8,"goal_node":9,"path":[8,9],"task_priority":3},
    ],"wait_allowed_nodes":[2,6],"zone_occupancy":{}}
    return graph, jobs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, help="Local UNMODIFIED Logistics_AI checkout (live mode)")
    ap.add_argument("--graph", type=Path, help="Route GeoJSON from that checkout")
    ap.add_argument("--jobs", type=Path, help="JSON with robots + optional path or route_result (preview result['path'])")
    ap.add_argument("--output", type=Path, default=Path("mapf_result.json"))
    ap.add_argument("--model", default="kev-08b")
    ap.add_argument("--deadline", type=float, default=0.15, help="deprecated compatibility option; live preview uses model catalog")
    ap.add_argument("--max-steps", type=int, default=None, help="deprecated; live preview uses model catalog")
    ap.add_argument("--max-rounds", type=int, default=16)
    ap.add_argument("--max-reroutes", type=int, default=3)
    modes = ap.add_mutually_exclusive_group()
    modes.add_argument("--demo", action="store_true", help="built-in offline mock demo; NO real KEV inference")
    modes.add_argument("--mock", action="store_true", help="offline decisions with supplied --graph and --jobs; NO real KEV inference")
    a = ap.parse_args(argv)
    if a.demo:
        graph, config = demo_data()
        live = MockKEV()
        print("[DEMO] No KEV model was called. Mock choices only.", file=sys.stderr)
    else:
        if not all([a.graph, a.jobs]):
            ap.error("non-demo mode requires --graph and --jobs")
        graph = Graph.load(a.graph)
        config = json.loads(a.jobs.read_text(encoding="utf-8"))
        if a.mock:
            live = MockKEV()
            print("[MOCK] No KEV model was called. Mock choices only.", file=sys.stderr)
        else:
            if a.repo is None:
                ap.error("live mode also requires --repo")
            live = LiveKEV(a.repo, model=a.model, deadline=a.deadline, max_steps=a.max_steps)
    if a.max_rounds < 1 or a.max_reroutes < 0:
        ap.error("invalid planning limits")
    robots = parse_robot_state(config, graph, live)
    result = KevMAPF(graph, robots, live, config, rounds=a.max_rounds, reroutes=a.max_reroutes).resolve()
    if a.demo:
        result["mode"] = "MOCK_DEMO_NOT_MODEL_INFERENCE"
    elif a.mock:
        result["mode"] = "MOCK_INPUT_NOT_MODEL_INFERENCE"
    else:
        result["mode"] = f"LIVE_{a.model}"
    text = json.dumps(result, ensure_ascii=False, indent=2)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"\nSaved advisory output to {a.output.resolve()}", file=sys.stderr)
    return 0 if result["status"] == "PROPOSED" else 2


if __name__ == "__main__":
    raise SystemExit(main())