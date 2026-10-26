"""[모듈: kev_adapter.py - FMS 연동 및 주문 경로 DTO 어댑터]
- 역할: FMS 및 Dispatcher의 주문 경로 데이터(`result['path']`)를 정규화하여 `KevMAPF` 엔진에 전달하고 결과를 반환한다.
- 기능:
  1. 단일 로봇 경로 결과 DTO 변환 (mapf_robot_from_route_result)
  2. 다중 로봇 상태·경로 검증 및 초기화 (parse_robot_state)
  3. 다중 주문 경로 결과 통합 MAPF 계획 수립 (plan_mapf_from_route_results)
"""

from __future__ import annotations

from typing import Any

from .kev_client import KevPlanner, LiveKEV
from .kev_engine import KevMAPF
from .kev_graph import Graph, _node


def mapf_robot_from_route_result(
    robot_id: str,
    current_node: int | str,
    goal_node: int | str,
    result: dict[str, Any],
    *,
    status: str = "IDLE",
    task_priority: int = 0,
    footprint_radius_m: float = 0.06,
    robot_type: str = "turtlebot3_burger",
) -> dict[str, Any]:
    """dispatcher의 실제 `result['path']`를 한 로봇의 MAPF 입력 딕셔너리로 변환한다."""
    if not isinstance(result, dict) or not isinstance(result.get("path"), list):
        raise ValueError("result must contain preview_llm_route() path list")
    return {
        "id": str(robot_id),
        "current_node": _node(current_node),
        "goal_node": _node(goal_node),
        "path": [_node(n) for n in result["path"]],
        "status": status,
        "task_priority": int(task_priority),
        "robot_type": robot_type,
        "footprint_radius_m": float(footprint_radius_m),
    }


def parse_robot_state(data: dict, graph: Graph, live: KevPlanner) -> dict[str, dict]:
    """입력 JSON의 `robots` 배열을 검증하고 로봇 ID별 정규화된 상태·경로 딕셔너리를 생성한다."""
    robots = {}
    for raw in data.get("robots", []):
        rid = str(raw["id"])
        if rid in robots:
            raise ValueError(f"duplicate robot id: {rid}")
        current, goal = _node(raw["current_node"]), _node(raw["goal_node"])
        if current not in graph.nodes or goal not in graph.nodes:
            raise ValueError(f"unknown start/goal of {rid}")

        candidates = [
            key
            for key in ("path", "route_result", "result")
            if raw.get(key) is not None
        ]
        if len(candidates) > 1:
            raise ValueError(f"{rid}: choose one of path, route_result or result (not multiple)")
        if candidates:
            source = candidates[0]
            raw_path = (
                raw["path"]
                if source == "path"
                else raw[source].get("path") if isinstance(raw[source], dict) else None
            )
            if not isinstance(raw_path, list) or not raw_path:
                raise ValueError(f"{rid}: {source} must contain a nonempty path list")
            path = [_node(n) for n in raw_path]
            path_source = "provided" if source == "path" else "preview_result"
        else:
            path_source = live.path_source
            path = live.route(
                graph.data,
                current,
                goal,
                selector=raw.get("selector", "kev"),
                model=raw.get("model"),
            )
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
            "robot_type": str(raw.get("robot_type", "turtlebot3_burger")),
            "footprint_radius_m": float(raw.get("footprint_radius_m", 0.06)),
            "execution_committed": bool(raw.get("execution_committed")),
            "occupied_edge": raw.get("occupied_edge"),
        }
    if not robots:
        raise ValueError("MAPF needs at least one robot")
    return robots


def plan_mapf_from_route_results(
    graph: dict | Graph,
    route_requests: list[dict],
    *,
    planner: KevPlanner | None = None,
    config: dict | None = None,
) -> dict:
    """`result['path']` 생성 이후, 실제 주행 요청 전에 사용하는 동기 MAPF 계획 함수."""
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
            order["robot_id"],
            request["current_node"],
            request["node"],
            request["result"],
            status=request.get("status", "IDLE"),
            task_priority=request.get("task_priority", 0),
        )
        robot.update(selector=order["selector"], model=order["model"])
        robot.update(execution_committed=bool(request.get("execution_committed")),
                     occupied_edge=request.get("occupied_edge"))
        if not robot["selector"] or not robot["model"]:
            raise ValueError("each order must specify its route selector and model")
        inputs.append(robot)

    robots = parse_robot_state({"robots": inputs}, route_graph, planner)
    for robot in robots.values():
        robot["source"] = "preview_result"

    if planner is None:
        planner = LiveKEV(model=settings.get("mapf_model", "kev-08b"))

    result = KevMAPF(
        route_graph, robots, planner, settings, rounds=rounds, reroutes=reroutes
    ).resolve()
    result["required_paths"] = {
        rid: [str(n) for n in path] for rid, path in result["paths"].items()
    }
    return result
