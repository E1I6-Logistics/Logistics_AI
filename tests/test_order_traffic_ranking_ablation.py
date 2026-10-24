"""고정 제안기가 실제 모델 호출로 집계되지 않고 기존 실행 검사를 거치는지 확인한다."""

import asyncio
from copy import deepcopy
from unittest.mock import Mock

import pytest

from backend.simulation import order_traffic_ranking_control as control
from tests.order_traffic_ranking_ablation import trace_summary


def test_fixed_order_preserves_strategy_ids_and_only_delayed_control_waits(monkeypatch):
    sleep=Mock();monkeypatch.setattr(control,"sleep",sleep)
    criteria={"KEEP_PATH":"keep","REROUTE_1":"first","REROUTE_2":"second"}
    result=control.FixedRankingSelector().select_choice({},criteria,"ignored")
    sleep.assert_not_called()
    from backend.app.services.order_traffic_strategy import probability_order
    assert probability_order(result,criteria)==list(criteria)
    delayed=control.FixedRankingSelector(.19)
    assert delayed.select_choice({},criteria,"ignored")==result
    sleep.assert_called_once_with(.19)
    assert delayed.last_inference is None and result["model_request_sent"] is False


@pytest.mark.parametrize("delay",[-1,float("nan"),float("inf"),6])
def test_invalid_delay_is_rejected(delay):
    with pytest.raises(ValueError):control.FixedRankingSelector(delay)


def test_fixed_trace_is_not_counted_as_model_inference():
    trace=[dict(event="REQUEST",ranking_source="MODEL",model_request_sent=True),
           dict(event="RESPONSE",ranking_source="MODEL",model_request_sent=True,elapsed_seconds=.2),
           dict(event="REQUEST",ranking_source="FIXED_CONTROL",model_request_sent=False),
           dict(event="RESPONSE",ranking_source="FIXED_CONTROL",model_request_sent=False,elapsed_seconds=.19)]
    result=trace_summary(trace)
    assert result["model"]==result["fixed"]=={"REQUEST":1,"RESPONSE":1}
    assert result["model_response_seconds"]==.2
    assert result["ranking_response_seconds"]==pytest.approx(.39)


from tests.test_order_traffic_service import env, moving_case, order


def test_fixed_control_keeps_latest_reservation_checks_and_never_creates_a_model(env,monkeypatch):
    from backend.app.services import order_traffic_service as common
    from backend.app.services import order_traffic_strategy as strategy
    from backend.app.services.route_selector import kev_selector
    store,_,graph=env;moving_case(store,graph)
    monkeypatch.setenv("FMS_TRAFFIC_SELECTION_POLICY","KEV_STRATEGY_ORDER")
    monkeypatch.setattr(common,"_select_choice_recorded",common._select_choice_recorded)
    monkeypatch.setattr(strategy,"coordinate_strategy_route",strategy.coordinate_strategy_route)
    actual=Mock(side_effect=AssertionError("고정 대조군은 모델을 만들면 안 됩니다."))
    monkeypatch.setattr(kev_selector,"KevSelector",actual)
    metadata=control.install_controls("FIXED_ORDER",.19)
    previous=deepcopy(store._robots["robot2"])
    row=order()
    result=asyncio.run(common.coordinate_order_route(row,"11","9",["9","7","11"],graph=graph,
                      source="EXISTING_SHORTEST_PATH",store=store))
    actual.assert_not_called()
    assert result["success"] and metadata["artificial_delay_seconds"]==0
    assert row["traffic_coordination"]["revalidated_before_apply"]
    assert store._robots["robot2"]==previous
    from tests.order_traffic_policy_comparison import load_lines
    trace=load_lines(common.LOG_DIR/"selection-inferences.jsonl")
    assert trace and all(t["ranking_source"]=="FIXED_CONTROL" and t["model_request_sent"] is False for t in trace)
