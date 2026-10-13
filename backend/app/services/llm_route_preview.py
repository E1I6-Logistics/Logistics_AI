"""Read-only local-model route preview using the live Route Graph."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from ..config import ROUTE_GRAPH_PATH
from .route_model_catalog import enabled_model_configuration
from .route_service import (
    build_edge_weight_lookup,
    build_compact_adjacency_graph,
    build_route_inputs,
    compare_path_metrics,
    plan_route,
    validate_and_calculate_path_distance,
)
from .iterative_route_runner import run_selected_route


def _validated_result(
    graph: dict[str, Any], path: list[int], start: int, target: int, model: str | None, calls: int
) -> dict[str, Any]:
    """선택한 경로를 검증하고 코드 최단경로와 거리·노드 순서를 비교한다."""
    points, edges = build_route_inputs(graph, require_stored_weight=True)
    checked_path, distance = validate_and_calculate_path_distance(points, edges, path, start, target)
    baseline = plan_route(str(start), str(target), graph, require_stored_weight=True)
    metrics = compare_path_metrics(
        [int(node) for node in baseline["node_ids"]], float(baseline["cost"]), checked_path, distance
    )
    return {
        "path": [str(node) for node in checked_path],
        "model": model,
        "total_weight": distance,
        "calls": calls,
        "baseline_path": [str(node) for node in baseline["node_ids"]],
        "baseline_weight": float(baseline["cost"]),
        **metrics,
    }


def preview_llm_route(
    graph: dict[str, Any],
    start_node: str | int,
    target_node: str | int,
    *,
    selector: str = "ollama",
    model: str | None = None,
    provider: Any | None = None,
) -> dict[str, Any]:
    """시험과 같은 상태·후보 규칙으로 경로를 선택하고 유효성을 검사한다."""
    load_dotenv(Path(__file__).resolve().parents[3] / "simulation" / ".env", override=False)
    if model is None:
        raise ValueError("경로 모델을 선택하세요.")
    model_config = enabled_model_configuration(selector, model)
    compact = build_compact_adjacency_graph(graph, ROUTE_GRAPH_PATH.name, require_stored_weight=True)
    nodes = sorted(int(node) for node in compact["nodes"])
    start, target = int(start_node), int(target_node)
    if start not in nodes or target not in nodes:
        raise ValueError("출발 또는 목적지 노드가 현재 Route Graph에 없습니다.")
    if start == target:
        return _validated_result(graph, [start], start, target, model, 0)

    # The benchmark also uses one selector call per multi-candidate step.
    if selector == "ollama":
        from .route_selector.ollama_selector import OllamaSelector

        if provider is None:
            from .llm_providers.ollama_provider import OllamaPathProvider

            provider = OllamaPathProvider(
                model=model,
                host=model_config["host"],
                timeout_seconds=float(model_config["timeout_seconds"]),
                options=dict(model_config["options"]),
                keep_alive=str(model_config["keep_alive"]),
                think=model_config["think"],
            )
        decision_selector = OllamaSelector(provider)
    elif selector == "laya":
        from .route_selector.laya_selector import LayaSelector

        decision_selector = LayaSelector(model=model)
    else:
        from .route_selector.kev_selector import KevSelector

        decision_selector = KevSelector(model=model)

    step_limit = int(model_config.get("max_steps") or len(nodes) * 2)
    if step_limit < 1:
        raise ValueError("최대 이동 단계는 양수여야 합니다.")
    points, edges = build_route_inputs(graph, require_stored_weight=True)
    prepared = {
        "compact_graph": compact,
        "nodes": nodes,
        "points": points,
        "edges": edges,
        "edge_weights": build_edge_weight_lookup(points, edges),
    }
    result = run_selected_route(
        decision_selector,
        prepared,
        {"start": start, "target": target},
        candidate_scope="neighbors",
        context_mode="neighbor_context",
        deadline=float(model_config.get("timeout_seconds", 60)),
        max_steps=step_limit,
    )
    if not result["valid_path"]:
        raise ValueError(result["error"] or "AI 경로 검증에 실패했습니다.")
    return _validated_result(graph, result["path"], start, target, model, result["api_call_count"])
