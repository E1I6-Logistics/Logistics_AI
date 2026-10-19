"""Shared next-node input and cycle policy for benchmarks and live previews."""

from __future__ import annotations

import math
from typing import Any

from .route_service import get_available_edges


# 모델 지시문 해석:
# 현재 노드에서 목적지까지 총 가중치가 가장 작은 유향 경로의 다음 노드 하나를 고른다.
# 바로 앞 간선의 가중치뿐 아니라 목적지까지 남은 비용을 고려하고, 저장된 가중치를 사용한다.
# 현재 노드에서 연결된 간선과 criteria에 있는 후보만 선택한다. available_edges가
# 있으면 그 목록에 있는 노드만 고른다. 사용한 간선은 다시 쓰지 않는다.
# 노드별 방문 상한을 지키며, backtrack_allowed일 때만 직전 노드로 돌아간다.
# 그보다 이전에 방문한 노드는 상한 내에서 재방문할 수 있다. 결과는 criteria ID만 반환한다.
NEXT_NODE_INSTRUCTIONS = (
    "Select exactly one next node for the minimum-total-weight directed route "
    "from current_node to target_node. Consider the estimated total remaining "
    "cost to the target, not merely the weight of the next edge. Use "
    "CompactAdjacencyGraph weights exactly as stored. The selected node must "
    "be connected by one directed edge from current_node and must be one of "
    "the supplied criteria. If available_edges is present, choose only a node "
    "listed there; it contains the exact outgoing edges and stored weights. "
    "Never reuse an edge in used_edges. Respect max_visits_per_node. Do not "
    "return to previous_node unless backtrack_allowed is true. Older nodes "
    "may be revisited within those limits. Return only the selected criteria ID."
)

MAX_VISITS_PER_NODE = 2
MAJOR_TASK_NODES = frozenset(range(7))

# 좌표 기반 방향·남은 직선거리 정보가 주어질 때 추가하는 지시문.
DIRECTION_INSTRUCTIONS = (
    "Use dist_to_target, progress (positive moves closer to target_node), "
    "and direction (compare with target_direction) to estimate remaining cost."
)

# traffic이 주어질 때만 NEXT_NODE_INSTRUCTIONS 뒤에 붙는 다른 로봇 고려 지시문 (시간 개념 없음).
TRAFFIC_INSTRUCTIONS = (
    "Other robots are shown in other_robots. Minimize total weight first; "
    "when candidates are similar, prefer a node whose shared_with is empty."
)

_COMPASS = ("E", "NE", "N", "NW", "W", "SW", "S", "SE")


def _compass(a: tuple[float, float], b: tuple[float, float]) -> str:
    """두 좌표 (x, y) 사이의 8방위 문자열을 반환한다."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    if math.isclose(dx, 0.0, abs_tol=1e-12) and math.isclose(dy, 0.0, abs_tol=1e-12):
        return "HERE"
    return _COMPASS[round(math.atan2(dy, dx) / (math.pi / 4)) % 8]


def _has_coords(
    points: dict[int, tuple[float, float] | None] | None, *nodes: int | None
) -> bool:
    return bool(points) and all(n is not None and points.get(int(n)) is not None for n in nodes)


def _apply_direction(
    available_edges: list[dict[str, Any]],
    current: int,
    target: int,
    points: dict[int, tuple[float, float] | None],
) -> list[dict[str, Any]]:
    """각 후보 간선에 목적지까지 직선거리(dist_to_target), 접근 변화량(progress), 이동 방위(direction)를 추가한다."""
    p_curr = points[current]
    p_target = points[target]
    assert p_curr is not None and p_target is not None
    curr_dist = math.dist(p_curr, p_target)
    enriched: list[dict[str, Any]] = []
    for edge in available_edges:
        node = int(edge["node"])
        p_next = points.get(node)
        if p_next is None:
            enriched.append(edge)
            continue
        next_dist = math.dist(p_next, p_target)
        enriched.append(
            {
                **edge,
                "dist_to_target": round(next_dist, 4),
                "progress": round(curr_dist - next_dist, 4),
                "direction": _compass(p_curr, p_next),
            }
        )
    return enriched


def _can_reach_target(
    compact_graph: dict[str, Any],
    current: int,
    next_node: int,
    target: int,
    used_edges: set[tuple[int, int]],
    visit_counts: dict[int, int],
    max_visits_per_node: int,
    forbidden_nodes: set[int] | frozenset[int],
) -> bool:
    """current -> next_node 이동 후 남은 허용 간선·방문 제한 안에서 target에 도달 가능한지 확인한다."""
    if next_node == target:
        return True
    if next_node in forbidden_nodes:
        return False
    blocked_edges = used_edges | {(current, next_node)}
    seen = {next_node}
    queue = [next_node]
    while queue:
        node = queue.pop(0)
        for edge in get_available_edges(compact_graph, node):
            nbr = int(edge["node"])
            if (node, nbr) in blocked_edges or nbr in forbidden_nodes:
                continue
            if visit_counts.get(nbr, 0) >= max_visits_per_node:
                continue
            if nbr == target:
                return True
            if nbr not in seen:
                seen.add(nbr)
                queue.append(nbr)
    return False


def _apply_traffic(
    available_edges: list[dict[str, Any]],
    candidate_nodes: list[int],
    target: int | None,
    traffic: dict[str, Any],
    *,
    compact_graph: dict[str, Any] | None = None,
    current: int | None = None,
    used_edges: set[tuple[int, int]] | None = None,
    visit_counts: dict[int, int] | None = None,
    max_visits_per_node: int = MAX_VISITS_PER_NODE,
    forbidden_major: frozenset[int] = frozenset(),
) -> tuple[list[dict[str, Any]], list[int], bool]:
    """다른 로봇의 현재 점유 노드(목적지 제외)를 후보에서 빼고, 남은 경로를 shared_with로 표시한다.

    필터 후 후보가 0개이거나 점유 노드를 피해서는 목적지에 도달할 수 없으면 기존 후보로 폴백한다.
    """
    occupied = {int(n) for n in traffic.get("occupied_nodes", {})}
    blocked = occupied - ({int(target)} if target is not None else set())

    def _viable(node: int) -> bool:
        if node in blocked:
            return False
        if (
            target is not None
            and compact_graph is not None
            and current is not None
            and used_edges is not None
            and visit_counts is not None
        ):
            return _can_reach_target(
                compact_graph,
                current,
                node,
                int(target),
                used_edges,
                visit_counts,
                max_visits_per_node,
                forbidden_major | blocked,
            )
        return True

    kept = [n for n in candidate_nodes if _viable(n)]
    fallback = not kept and bool(candidate_nodes)
    if not fallback:
        kept_set = set(kept)
        candidate_nodes = kept
        available_edges = [e for e in available_edges if int(e["node"]) in kept_set]

    sharers: dict[int, list[str]] = {}
    for rid, robot_path in sorted(traffic.get("robot_paths", {}).items()):
        for n in {int(x) for x in robot_path}:
            sharers.setdefault(n, []).append(str(rid))
    available_edges = [{**e, "shared_with": sharers.get(int(e["node"]), [])} for e in available_edges]
    return available_edges, candidate_nodes, fallback


# 다음 노드 후보를 만들고, 직전 노드로 되돌아갈 수 있는지 결정
def next_node_options(
    compact_graph: dict[str, Any],
    nodes: list[int],
    current: int,
    path: list[int],
    used_edges: set[tuple[int, int]],
    visit_counts: dict[int, int],
    *,
    candidate_scope: str = "neighbors",
    max_visits_per_node: int = MAX_VISITS_PER_NODE,
    target: int | None = None,
    traffic: dict[str, Any] | None = None,
    points: dict[int, tuple[float, float] | None] | None = None,
) -> dict[str, Any]:
    """
    이미 사용한 방향성 간선·방문 상한을 넘는 노드는 제외한다.
    직전 노드는 다른 후보가 없을 때만 허용한다.
    neighbors는 연결 노드만, all은 전체 노드를 후보로 둔다.
    points와 target이 주어지면 후보 간선에 목적지 방향·거리 힌트를 붙인다.
    traffic이 있으면 다른 로봇 점유 노드를 제외하고 shared_with를 붙인다 (없으면 기존과 동일).
    """
    if candidate_scope not in {"neighbors", "all"}:
        raise ValueError(f"Unsupported candidate_scope: {candidate_scope}")

    previous_node = path[-2] if len(path) >= 2 else None
    raw_edges = get_available_edges(compact_graph, current)
    forbidden_major = (
        MAJOR_TASK_NODES - {int(target)}
        if target is not None and MAJOR_TASK_NODES.issubset(nodes)
        else frozenset()
    )

    # 주어진 경로 선택이 허용되는지 확인한다.
    def allowed(node: int, *, allow_previous: bool, skip_major: bool = True, check_reach: bool = True) -> bool:
        if (
            node == current
            or (skip_major and node in forbidden_major)
            or (not allow_previous and node == previous_node)
            or (current, node) in used_edges
            or visit_counts.get(node, 0) >= max_visits_per_node
        ):
            return False
        if check_reach and target is not None and candidate_scope == "neighbors":
            return _can_reach_target(
                compact_graph,
                current,
                node,
                int(target),
                used_edges,
                visit_counts,
                max_visits_per_node,
                forbidden_major if skip_major else frozenset(),
            )
        return True

    strict_edges = [edge for edge in raw_edges if allowed(int(edge["node"]), allow_previous=False)]
    recovery_edges = [edge for edge in raw_edges if allowed(int(edge["node"]), allow_previous=True)]
    if not strict_edges and not recovery_edges and forbidden_major:
        strict_edges = [
            edge for edge in raw_edges if allowed(int(edge["node"]), allow_previous=False, skip_major=False)
        ]
        recovery_edges = [
            edge for edge in raw_edges if allowed(int(edge["node"]), allow_previous=True, skip_major=False)
        ]
    if not strict_edges and not recovery_edges:
        strict_edges = [
            edge
            for edge in raw_edges
            if allowed(int(edge["node"]), allow_previous=False, skip_major=False, check_reach=False)
        ]
        recovery_edges = [
            edge
            for edge in raw_edges
            if allowed(int(edge["node"]), allow_previous=True, skip_major=False, check_reach=False)
        ]
    backtrack_allowed = not strict_edges and bool(recovery_edges)
    available_edges = recovery_edges if backtrack_allowed else strict_edges

    if candidate_scope == "neighbors":
        candidate_nodes = [int(edge["node"]) for edge in available_edges]
    else:
        strict_nodes = [node for node in nodes if allowed(node, allow_previous=False, check_reach=False)]
        recovery_nodes = [node for node in nodes if allowed(node, allow_previous=True, check_reach=False)]
        if not strict_nodes and not recovery_nodes and forbidden_major:
            strict_nodes = [
                node for node in nodes if allowed(node, allow_previous=False, skip_major=False, check_reach=False)
            ]
            recovery_nodes = [
                node for node in nodes if allowed(node, allow_previous=True, skip_major=False, check_reach=False)
            ]
        backtrack_allowed = not strict_nodes and bool(recovery_nodes)
        candidate_nodes = recovery_nodes if backtrack_allowed else strict_nodes

    prevented = len(raw_edges) - len(available_edges)
    if _has_coords(points, current, target):
        assert points is not None and target is not None
        available_edges = _apply_direction(available_edges, current, int(target), points)

    traffic_fallback = False
    if traffic is not None:
        available_edges, candidate_nodes, traffic_fallback = _apply_traffic(
            available_edges,
            candidate_nodes,
            target,
            traffic,
            compact_graph=compact_graph,
            current=current,
            used_edges=used_edges,
            visit_counts=visit_counts,
            max_visits_per_node=max_visits_per_node,
            forbidden_major=forbidden_major,
        )

    result = {
        "previous_node": previous_node,
        "available_edges": available_edges,
        "candidate_nodes": candidate_nodes,
        "backtrack_allowed": backtrack_allowed,
        "prevented_candidate_count": prevented,
    }
    if traffic is not None:
        result["traffic_fallback"] = traffic_fallback
    return result


# 모델이 다음 노드를 선택할 때 사용할 입력 데이터 구성
def next_node_state(
    compact_graph: dict[str, Any],
    current: int,
    target: int,
    path: list[int],
    used_edges: set[tuple[int, int]],
    visit_counts: dict[int, int],
    accumulated_distance: float,
    options: dict[str, Any],
    *,
    context_mode: str = "neighbor_context",
    max_visits_per_node: int = MAX_VISITS_PER_NODE,
    traffic: dict[str, Any] | None = None,
    points: dict[int, tuple[float, float] | None] | None = None,
) -> dict[str, Any]:
    """
    전체 그래프·현재/목표 노드·방문/사용 간선·누적 거리를 담고,
    neighbor_context일 때만 현재 노드의 available_edges를 추가한다.
    points가 있으면 현재 노드에서 목적지까지의 직선거리와 방위를 추가한다.
    traffic이 있으면 other_robots(현재 위치·남은 경로)를 추가한다.
    """
    if context_mode not in {"neighbor_context", "full_graph_only"}:
        raise ValueError(f"Unsupported context_mode: {context_mode}")

    state = {
        "route_graph": compact_graph,
        "current_node": current,
        "previous_node": options["previous_node"],
        "target_node": target,
        "visited_nodes": path,
        "visit_counts": dict(sorted(visit_counts.items())),
        "max_visits_per_node": max_visits_per_node,
        "used_edges": [{"from": start, "to": end} for start, end in sorted(used_edges)],
        "backtrack_allowed": options["backtrack_allowed"],
        "accumulated_distance": accumulated_distance,
    }

    if _has_coords(points, current, target):
        assert points is not None
        p_curr, p_target = points[current], points[target]
        assert p_curr is not None and p_target is not None
        state["target_distance"] = round(math.dist(p_curr, p_target), 4)
        state["target_direction"] = _compass(p_curr, p_target)

    if context_mode == "neighbor_context":
        state["available_edges"] = options["available_edges"]
    if traffic is not None:
        positions: dict[str, list[int]] = {}
        for node, rid in traffic.get("occupied_nodes", {}).items():
            positions.setdefault(str(rid), []).append(int(node))
        paths = {str(k): v for k, v in traffic.get("robot_paths", {}).items()}
        state["other_robots"] = [
            {
                "robot_id": rid,
                "occupied_nodes": sorted(positions.get(rid, [])),
                "remaining_path": [int(n) for n in paths.get(rid, [])],
            }
            for rid in sorted(set(positions) | set(paths))
        ]
    return state
