"""교통조정이 기존 경로 생성기에 보내는 공통 요청. 예약표·원본 지도는 수정하지 않는다."""

from ..route_service import plan_route


def generate_traffic_route(graph, start, goal, *, source, selector="kev", model="kev-08b",
                           blocked_nodes=(), blocked_edges=()):
    constraints = dict(blocked_nodes=blocked_nodes, blocked_edges=blocked_edges)
    if source == "KEV":
        from ..llm_route_preview import preview_llm_route

        result = preview_llm_route(graph, start, goal, selector=selector, model=model, **constraints)
    elif source == "EXISTING_SHORTEST_PATH":
        result = dict(path=plan_route(start, goal, graph, **constraints)["node_ids"], calls=0)
    else:
        raise ValueError(f"지원하지 않는 경로 생성 방식: {source}")
    return {**result, "source": source,
            "blocked_nodes": list(map(str, blocked_nodes)),
            "blocked_edges": [list(map(str, pair)) for pair in blocked_edges]}
