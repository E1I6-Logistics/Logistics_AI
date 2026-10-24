"""새 요청의 전략 순서 제안이 기존 이동·예약·취소 경계를 지키는지 검사한다."""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from tests.test_order_traffic_service import env, moving_case, order, place
from backend.app.services import order_traffic_service as service
from backend.app.services import order_traffic_strategy as strategy


class Ranker:
    name = "kev"

    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback

    def select_choice(self, state, criteria, instructions, **kwargs):
        self.calls.append((deepcopy(state), deepcopy(criteria)))
        if self.callback:
            self.callback()
        keys = list(criteria)
        weights = list(range(len(keys), 0, -1))
        return dict(choice=keys[0], probabilities={k:w/sum(weights) for k,w in zip(keys,weights)})


@pytest.fixture(autouse=True)
def choose_strategy(monkeypatch):
    monkeypatch.setattr(service, "_simulation_policy_override", None)
    monkeypatch.setenv("FMS_TRAFFIC_STRATEGY_CALL_LIMIT", "8")


def enable(monkeypatch):
    monkeypatch.setenv("FMS_TRAFFIC_SELECTION_POLICY", "KEV_STRATEGY_ORDER")


def test_strategies_are_ranked_before_generation_and_other_plans_are_preserved(env, monkeypatch):
    store, _, graph = env
    enable(monkeypatch)
    moving_case(store, graph)
    before = deepcopy(store._robots["robot2"])
    reservations = tuple(r for r in store._traffic._reservations.snapshot() if r.robot_id == "robot2")
    generated = Mock(wraps=strategy.generate_traffic_route)
    monkeypatch.setattr(strategy, "generate_traffic_route", generated)
    ranker = Ranker(callback=lambda: generated.assert_not_called())
    row = order()
    result = asyncio.run(service.coordinate_order_route(row,"11","9",["9","7","11"],graph=graph,
                        source="EXISTING_SHORTEST_PATH",store=store,selector=ranker))
    assert result["success"] and len(ranker.calls) == 1
    assert generated.call_count > 0
    assert all("path" not in json.loads(spec) for spec in ranker.calls[0][1].values())
    assert any(r["goal_node"] == "5" for r in ranker.calls[0][0]["robots"])
    assert store._robots["robot2"] == before
    assert tuple(r for r in store._traffic._reservations.snapshot() if r.robot_id == "robot2") == reservations
    event = row["traffic_coordination"]
    assert event["selected_by"] == "KEV_STRATEGY_ORDER_CODE_MIN_TIME"
    assert event["revalidated_before_apply"] and event["existing_plans_preserved"]
    assert event["candidates"][event["choice"]]["estimated_metrics"]["new_request_completion_seconds"] == min(
        c["estimated_metrics"]["new_request_completion_seconds"] for c in event["candidates"].values())


def test_blocked_first_strategy_is_not_executed_and_detour_is_checked(env, monkeypatch):
    store, _, graph = env
    enable(monkeypatch)
    place(store,"robot1","9",graph);place(store,"robot2","7",graph)
    ranker=Ranker(); row=order()
    asyncio.run(service.coordinate_order_route(row,"11","9",["9","7","11"],graph=graph,
                source="EXISTING_SHORTEST_PATH",store=store,selector=ranker))
    event=row["traffic_coordination"]
    assert len(ranker.calls)==1 and "7" not in event["path"]
    assert event["strategy_batches"][0]["attempts"][0]["result"]=="NO_FEASIBLE_RESERVATION"
    assert store._robots["robot2"]["occupied_node"]=="7"


@pytest.mark.parametrize("change",["cancel","map","mode","position","probabilities"])
def test_stale_or_invalid_strategy_cannot_start_a_robot(env,monkeypatch,change):
    store,mode,graph=env;enable(monkeypatch);moving_case(store,graph);valid=[True]
    def mutate():
        if change=="cancel":valid[0]=False
        if change=="map":monkeypatch.setattr(service,"load_route_graph",lambda:{})
        if change=="mode":mode.operation_mode="algorithm"
        if change=="position":place(store,"robot1","10",graph)
    ranker=Ranker(mutate)
    if change=="probabilities":ranker.select_choice=lambda *a,**k:dict(choice="KEEP_PATH",probabilities={})
    with pytest.raises(ValueError):
        asyncio.run(service.coordinate_order_route(order(),"11","9",["9","7","11"],graph=graph,
                    source="EXISTING_SHORTEST_PATH",store=store,selector=ranker,valid=lambda:valid[0]))
    assert store._robots["robot1"]["route"] is None


def test_unchanged_snapshot_reuses_ranking_after_commit_race(env,monkeypatch):
    store,_,graph=env;enable(monkeypatch);moving_case(store,graph)
    original=store._traffic.commit_new_route;count=[0]
    def commit(*a,**k):
        count[0]+=1
        return False if count[0]==1 else original(*a,**k)
    monkeypatch.setattr(store._traffic,"commit_new_route",commit)
    ranker=Ranker();row=order()
    asyncio.run(service.coordinate_order_route(row,"11","9",["9","7","11"],graph=graph,
                source="EXISTING_SHORTEST_PATH",store=store,selector=ranker))
    assert len(ranker.calls)==1
    assert row["traffic_coordination"]["strategy_batches"][-1]["reused"]


def test_snapshot_identity_changes_when_other_robot_or_reservation_changes(env):
    store,_,graph=env
    before=strategy.snapshot_key(store._traffic,graph)
    place(store,"robot2","7",graph)
    assert strategy.snapshot_key(store._traffic,graph)!=before
    before=strategy.snapshot_key(store._traffic,graph)
    store.navigate_to_node("robot2","5",required_path=["7","5"])
    assert strategy.snapshot_key(store._traffic,graph)!=before


def test_same_goal_keeps_existing_no_movement_behavior(env,monkeypatch):
    store,_,graph=env;enable(monkeypatch);ranker=Ranker()
    result=asyncio.run(service.coordinate_order_route(order(),"0","0",["0"],graph=graph,
                       source="EXISTING_SHORTEST_PATH",store=store,selector=ranker))
    assert result["success"] and not ranker.calls


def test_cancellation_during_model_call_never_commits(env,monkeypatch):
    import threading
    store,_,graph=env;enable(monkeypatch);moving_case(store,graph)
    entered,release=threading.Event(),threading.Event();ranker=Ranker()
    original=ranker.select_choice
    def choose(*a,**k):
        entered.set();release.wait(2)
        return original(*a,**k)
    ranker.select_choice=choose;row=order()
    async def run():
        task=asyncio.create_task(service.coordinate_order_route(row,"11","9",["9","7","11"],graph=graph,
                                 source="EXISTING_SHORTEST_PATH",store=store,selector=ranker))
        while not entered.is_set():await asyncio.sleep(.01)
        task.cancel();release.set()
        with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(run())
    assert row["traffic_coordination"]["state"]=="CANCELLED"
    assert store._robots["robot1"]["route"] is None


def test_changed_snapshot_requires_new_ranking_and_respects_call_cap(env,monkeypatch):
    store,_,graph=env;enable(monkeypatch);moving_case(store,graph)
    monkeypatch.setenv("FMS_TRAFFIC_STRATEGY_CALL_LIMIT","1")
    def race(*a,**k):
        place(store,"robot3","1",graph)
        return False
    monkeypatch.setattr(store._traffic,"commit_new_route",race)
    ranker=Ranker()
    with pytest.raises(ValueError,match="호출 상한"):
        asyncio.run(service.coordinate_order_route(order(),"11","9",["9","7","11"],graph=graph,
                    source="EXISTING_SHORTEST_PATH",store=store,selector=ranker))
    assert len(ranker.calls)==1 and store._robots["robot1"]["route"] is None


def test_policy_switch_is_simulation_only_and_rejects_active_orders(env,monkeypatch):
    from backend.app.routers import mode as router
    from backend.app.services import order_manager,scenario_service
    store,mode,_=env
    manager=SimpleNamespace(list_orders=lambda:[],returning={},recovering={})
    monkeypatch.setattr(router,"mode_manager",mode)
    monkeypatch.setattr(order_manager,"order_manager",manager)
    monkeypatch.setattr(scenario_service,"scenario_manager",SimpleNamespace(ensure_available=lambda:None))
    payload=router.OrderTrafficRequest(selection_policy="KEV_STRATEGY_ORDER")
    result=asyncio.run(router.set_order_traffic(payload))
    assert result["order_traffic"]["selection_policy"]=="KEV_STRATEGY_ORDER"
    manager.list_orders=lambda:[dict(status="PENDING")]
    with pytest.raises(HTTPException,match="409"):
        asyncio.run(router.set_order_traffic(router.OrderTrafficRequest(selection_policy="KEV")))
    assert service.selection_policy()=="KEV_STRATEGY_ORDER"
    manager.list_orders=lambda:[];mode.mode="real"
    with pytest.raises(HTTPException,match="409"):
        asyncio.run(router.set_order_traffic(payload))
