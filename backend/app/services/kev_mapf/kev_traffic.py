"""[모듈: kev_traffic.py - MAPF 우회·복구용 로봇 점유 및 잔여 경로 수집]
- 역할:
  1. FMS 스냅샷에서 대상 로봇을 제외한 다른 로봇의 점유 노드·점유 간선 양 끝 노드와 주행·대기 중인 로봇의 남은 경로를 수집한다 (`live_traffic`).
  2. KevMAPF가 보유한 다른 로봇의 위치·경로를 동일한 `traffic` 구조로 변환한다 (`mapf_traffic`).
  3. MAPF 우회·복구에서는 수집한 점유 정보를 이용해 금지 노드를 제거한 그래프를 구성한다.
     경로 모델에는 이 그래프를 전달하며, 점유·남은 경로 스냅샷을 다음 노드 context에
     직접 추가하거나 `preview_llm_route(..., traffic=...)`로 전달하지 않는다.
  4. ETA·속도·예약 시간 구간은 생성하지 않는다. 시간 기반 예약과 실행 허가는 하위 FMS가 담당한다.
"""

from __future__ import annotations

from typing import Any

from .kev_graph import _node


def _edge_endpoints(occupied_edge: Any, route: Any, graph: dict[str, Any] | None) -> list[int]:
    """간선 점유 중인 로봇의 양 끝 노드를 추출한다."""
    endpoints: list[int] = []
    if isinstance(occupied_edge, (list, tuple)) and len(occupied_edge) == 2:
        return [_node(occupied_edge[0]), _node(occupied_edge[1])]

    if isinstance(route, dict):
        nodes = route.get("node_ids")
        idx = route.get("segment_index")
        if (
            isinstance(nodes, list)
            and isinstance(idx, int)
            and not isinstance(idx, bool)
            and 0 <= idx < len(nodes) - 1
        ):
            endpoints.extend([_node(nodes[idx]), _node(nodes[idx + 1])])

    if not endpoints and isinstance(graph, dict):
        edge_id = str(occupied_edge)
        for feat in graph.get("features", []):
            props = feat.get("properties") or {}
            if str(props.get("id")) == edge_id and "startid" in props and "endid" in props:
                endpoints.extend([_node(props["startid"]), _node(props["endid"])])
                break
    return endpoints


def live_traffic(
    exclude_robot_id: str | int,
    *,
    graph: dict[str, Any] | None = None,
    snapshots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """FMS 시뮬레이션에서 대상 로봇을 제외한 현재 점유 노드와 Active 로봇의 남은 경로를 수집한다.

    - 모든 타 로봇의 현재 점유 노드/간선 양 끝 노드는 `occupied_nodes`에 포함 (하드 제외용).
    - 주행 중(`NAVIGATING`, `WAITING`)인 Active 로봇의 남은 경로만 `robot_paths`에 포함 (소프트 정보용, Pending 제외).
    """
    if snapshots is None:
        from ..mock_data import mock_fms

        snapshots = mock_fms.robot_snapshots("simulation")

    exclude = str(exclude_robot_id)
    occupied_nodes: dict[int, str] = {}
    robot_paths: dict[str, list[int]] = {}

    for robot in snapshots:
        rid = str(robot.get("robot_id"))
        if rid == exclude:
            continue

        occ_node = robot.get("occupied_node")
        occ_edge = robot.get("occupied_edge")
        route = robot.get("route")

        if occ_node is not None:
            occupied_nodes[_node(occ_node)] = rid
        if occ_edge is not None:
            for ep in _edge_endpoints(occ_edge, route, graph):
                occupied_nodes[ep] = rid

        if robot.get("status") in ("NAVIGATING", "WAITING") and isinstance(route, dict):
            nodes = route.get("node_ids")
            idx = route.get("segment_index")
            if (
                isinstance(nodes, list)
                and isinstance(idx, int)
                and not isinstance(idx, bool)
                and 0 <= idx < len(nodes)
            ):
                robot_paths[rid] = [_node(n) for n in nodes[idx:]]

    return {
        "occupied_nodes": occupied_nodes,
        "robot_paths": robot_paths,
    }


def mapf_traffic(
    robots: dict[str, dict[str, Any]],
    exclude_robot_id: str | int,
    *,
    occupied: dict[str, str] | None = None,
) -> dict[str, Any]:
    """KevMAPF 내부 로봇 사전에서 우회 대상 로봇을 제외한 타 로봇 위치·경로를 `traffic` 형식으로 변환한다."""
    exclude = str(exclude_robot_id)
    occupied_nodes: dict[int, str] = {}
    robot_paths: dict[str, list[int]] = {}

    for rid, robot in sorted(robots.items()):
        rid_str = str(rid)
        if rid_str == exclude:
            continue
        if robot.get("current_node") is not None:
            occupied_nodes[_node(robot["current_node"])] = rid_str
        if robot.get("path"):
            robot_paths[rid_str] = [_node(n) for n in robot["path"]]

    for resource, owner in (occupied or {}).items():
        owner_str = str(owner)
        if owner_str != exclude and str(resource).startswith("node:"):
            occupied_nodes[_node(str(resource).removeprefix("node:"))] = owner_str

    return {
        "occupied_nodes": occupied_nodes,
        "robot_paths": robot_paths,
    }
