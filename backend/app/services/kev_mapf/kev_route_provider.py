"""[모듈: kev_route_provider.py - KEV 트래픽 회피 경로 생성 제공자]
- 역할: 장애물/차단 노드·간선 제약 조건을 반영하여 KEV 또는 최단 경로를 생성한다.
"""

from __future__ import annotations

from typing import Any

from ..llm_route_preview import preview_llm_route
from ..route_service import plan_route


def generate_traffic_route(
    graph: dict[str, Any],
    start: int | str,
    goal: int | str,
    *,
    source: str = "KEV",
    selector: str = "kev",
    model: str = "kev-08b",
    blocked_nodes: tuple | list = (),
    blocked_edges: tuple | list = (),
) -> dict[str, Any]:
    """장애물 제약 조건을 주입하여 KEV AI 또는 최단 경로를 생성하고 결과를 표준 형식으로 반환한다."""
    constraints = dict(blocked_nodes=blocked_nodes, blocked_edges=blocked_edges)
    if source == "KEV":
        result = preview_llm_route(
            graph, start, goal, selector=selector, model=model, **constraints
        )
    elif source == "EXISTING_SHORTEST_PATH":
        result = dict(
            path=plan_route(start, goal, graph, **constraints)["node_ids"],
            calls=0,
        )
    else:
        raise ValueError(f"지원하지 않는 경로 생성 방식: {source}")

    return {
        **result,
        "source": source,
        "blocked_nodes": list(map(str, blocked_nodes)),
        "blocked_edges": [list(map(str, pair)) for pair in blocked_edges],
    }


__all__ = ["generate_traffic_route"]
