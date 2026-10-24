"""시뮬 비교가 기존 알고리즘 경로를 우리 교통조정으로 바꿔치기하지 않는지 확인한다."""

import asyncio
from copy import deepcopy
from unittest.mock import Mock

import pytest

from tests.order_traffic_strategy_smoke import order_payload, validate_mode
from tests.order_traffic_native_mode_comparison import order_identity
from tests.order_traffic_native_mode_comparison import traffic_branch_verified
from tests.test_order_traffic_service import env, order


def test_native_order_bypasses_added_planner_and_kev_even_when_feature_flags_are_on(env, monkeypatch):
    from backend.app.services import order_route_dispatcher as dispatcher, order_traffic_service as traffic
    from backend.app.services import traffic_route_provider, llm_route_preview
    store, mode, _ = env
    mode.operation_mode = "algorithm"
    row = {**order(), "operation_mode": "algorithm", "driving_mode": "standard", "selector": None, "model": None}
    forbidden = Mock(side_effect=AssertionError("기존 알고리즘은 추가 교통조정·AI 경로를 호출하면 안 됩니다."))
    monkeypatch.setattr(traffic, "coordinate_order_route", forbidden)
    monkeypatch.setattr(traffic_route_provider, "generate_traffic_route", forbidden)
    monkeypatch.setattr(llm_route_preview, "preview_llm_route", forbidden)
    original = store.navigate_to_node
    navigate = Mock(wraps=original)
    monkeypatch.setattr(store, "navigate_to_node", navigate)
    result = asyncio.run(dispatcher.dispatch_order_route(row, "5", "0"))
    assert result["success"]
    navigate.assert_called_once_with("robot1", "5", required_path=None)
    forbidden.assert_not_called()
    assert store._robots["robot1"]["goal_node"] == "5"


def test_native_preflight_rejects_accidentally_enabled_added_traffic():
    mode = dict(simulation_preview=True, mode="simulation", operation_mode="algorithm",
                simulation_preview_run_id="own", order_traffic=dict(enabled=False, selection_policy=None, route_source="MODE"))
    validate_mode(mode, "algorithm", "KEV_STRATEGY_ORDER", "own")
    wrong = deepcopy(mode)
    wrong["order_traffic"].update(enabled=True, selection_policy="MIN_EXPECTED_TIME")
    with pytest.raises(AssertionError):
        validate_mode(wrong, "algorithm", "KEV_STRATEGY_ORDER", "own")
    with pytest.raises(AssertionError):
        validate_mode(mode, "algorithm", "KEV_STRATEGY_ORDER", "other")


@pytest.mark.parametrize("operation,driving,selector,model", [
    ("algorithm", "standard", None, None), ("ai", "llm", "kev", "kev-08b")])
def test_payload_and_saved_order_identity_distinguish_native_and_our_ai(operation, driving, selector, model):
    payload = order_payload(0, dict(A=1, B=0, C=1, D=0), operation)
    assert payload["total_quantity"] == 2
    assert payload.get("selector") == selector and payload.get("model") == model
    stored = {**payload, "mode": "simulation", "driving_mode": driving}
    assert order_identity([stored], operation)
    stored["operation_mode"] = "ai" if operation == "algorithm" else "algorithm"
    assert not order_identity([stored], operation)
    assert not order_identity([], operation)


def test_ai_preflight_keeps_existing_default_requirements():
    mode = dict(simulation_preview=True, mode="simulation", operation_mode="ai",
                order_traffic=dict(enabled=True, selection_policy="KEV_STRATEGY_ORDER", route_source="EXISTING_SHORTEST_PATH"))
    validate_mode(mode, "ai", "KEV_STRATEGY_ORDER")
    mode["order_traffic"]["route_source"] = "KEV"
    with pytest.raises(AssertionError):
        validate_mode(mode, "ai", "KEV_STRATEGY_ORDER")


def test_ai_mode_can_legitimately_need_no_model_call_when_only_one_strategy_is_available():
    decision = dict(selection_policy="KEV_STRATEGY_ORDER", route_source="EXISTING_SHORTEST_PATH",
                    selected_by="SINGLE_OPTION", kev_calls=0)
    assert traffic_branch_verified("ai", [decision], [])
    assert not traffic_branch_verified("algorithm", [decision], [])
    assert traffic_branch_verified("algorithm", [], [])
    assert not traffic_branch_verified("ai", [], [])
