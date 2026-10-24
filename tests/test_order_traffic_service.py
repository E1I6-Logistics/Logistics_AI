"""일반 주문 연결의 예약 보존·실제 우회 생성·취소·상태 변화 검증."""

import asyncio
from copy import deepcopy
import json
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.app.services import mock_data, mode_service, order_route_dispatcher, order_traffic_service as service
from backend.app.services.pathfinding import DistanceAStar
from backend.app.services.reservation import ReservationTable, Reservation, node_key, edge_key
from backend.app.services.route_graph import load_route_graph
from backend.app.services.route_service import plan_route, constrained_route_graph
from backend.app.services.traffic_route_provider import generate_traffic_route


@pytest.fixture
def env(monkeypatch, tmp_path):
    store = mock_data.MockFmsStore(ReservationTable())
    mode = SimpleNamespace(mode="simulation", operation_mode="ai")
    monkeypatch.setattr(mock_data, "mock_fms", store)
    monkeypatch.setattr(mode_service, "mode_manager", mode)
    monkeypatch.setattr(order_route_dispatcher, "mode_manager", mode)
    monkeypatch.setattr(service, "LOG_DIR", tmp_path)
    monkeypatch.setenv("FMS_ORDER_TRAFFIC_ENABLED", "1")
    monkeypatch.setenv("FMS_ORDER_ROUTE_SOURCE", "EXISTING_SHORTEST_PATH")
    monkeypatch.setenv("FMS_TRAFFIC_SELECTION_POLICY", "KEV")
    return store, mode, load_route_graph()


def place(store, rid, node, graph):
    x, y = DistanceAStar(graph).nodes[node]
    store.navigate_to_pose(rid, x, y)


def order(rid="robot1"):
    return dict(order_id="trial", robot_id=rid, mode="simulation", operation_mode="ai",
                driving_mode="llm", selector="kev", model="kev-08b", phase="MOVING_TO_PICKUP")


def test_constraints_use_existing_planner_and_preserve_original_map(env):
    _, _, graph = env
    before = deepcopy(graph)
    assert plan_route("9", "11", graph)["node_ids"] == ["9", "7", "11"]
    result = generate_traffic_route(graph, "9", "11", source="EXISTING_SHORTEST_PATH", blocked_nodes=["7"])
    assert "7" not in result["path"] and result["path"][-1] == "11"
    blocked = constrained_route_graph(graph, "9", "11", blocked_edges=[("9", "7")])
    for f in blocked["features"]:
        props = f.get("properties", {})
        assert {str(props.get("startid")), str(props.get("endid"))} != {"9", "7"}
    assert graph == before
    with pytest.raises(ValueError, match="출발·목적지"):
        plan_route("9", "11", graph, blocked_nodes=["11"])


def test_ai_reroute_calls_existing_ai_generator_with_constraints(env, monkeypatch):
    _, _, graph = env
    from backend.app.services import llm_route_preview
    generated = Mock(return_value=dict(path=["9", "10", "8", "12", "11"], calls=3))
    monkeypatch.setattr(llm_route_preview, "preview_llm_route", generated)
    result = generate_traffic_route(graph, "9", "11", source="KEV", blocked_nodes=["7"])
    assert result["calls"] == 3
    assert generated.call_args.kwargs["blocked_nodes"] == ["7"]


def test_new_route_preview_and_commit_preserve_other_moving_robot(env):
    store, _, graph = env
    store.navigate_to_node("robot2", "5", required_path=["1", "9", "7", "5"])
    before = deepcopy(store._robots["robot2"])
    rows = tuple(r for r in store._traffic._reservations.snapshot() if r.robot_id == "robot2")
    path = ["0", "10", "8", "12", "11"]
    all_before = deepcopy(store._robots)
    assert store._traffic.preview_new_route("robot1", path, now=monotonic(), graph=graph)
    assert store._robots == all_before
    assert store._traffic.commit_new_route("robot1", path, now=monotonic(), graph=graph)
    assert store._robots["robot2"] == before
    assert tuple(r for r in store._traffic._reservations.snapshot() if r.robot_id == "robot2") == rows


def test_failed_preflight_preserves_all_state_and_reservations(env):
    store, _, graph = env
    place(store, "robot1", "9", graph)
    place(store, "robot2", "7", graph)
    before, rows = deepcopy(store._robots), store._traffic._reservations.snapshot()
    assert not store._traffic.commit_new_route("robot1", ["9", "7", "11"], now=monotonic(), graph=graph)
    assert store._robots == before and store._traffic._reservations.snapshot() == rows


def test_normal_dispatch_uses_generated_detour_when_parked_robot_blocks(env):
    store, _, graph = env
    place(store, "robot1", "9", graph)
    place(store, "robot2", "7", graph)
    row = order()
    result = asyncio.run(order_route_dispatcher.dispatch_order_route(row, "11", "9", valid=lambda: True))
    assert result["success"]
    detail = row["traffic_coordination"]
    assert detail["state"] == "APPLIED" and detail["choice"].startswith("REROUTE")
    assert detail["selected_by"] == "SINGLE_OPTION" and detail["kev_calls"] == 0
    assert "7" not in detail["path"]
    assert detail["reroute_generation"][0]["blocked_nodes"] == ["7"]
    assert store._robots["robot2"]["occupied_node"] == "7"


class Choosing:
    name = "kev"

    def __init__(self, callback=None, invalid=False):
        self.calls, self.callback, self.invalid = [], callback, invalid

    def select_choice(self, state, candidates, instructions, **kwargs):
        self.calls.append((state, candidates))
        if self.callback:
            self.callback()
        return dict(choice="BAD" if self.invalid else min(candidates, key=lambda k:
                    json.loads(candidates[k])["estimated_metrics"]["new_request_completion_seconds"]))


def moving_case(store, graph):
    place(store, "robot1", "9", graph)
    place(store, "robot2", "7", graph)
    store.navigate_to_node("robot2", "5", required_path=["7", "5"])


def test_multiple_options_call_kev_once_and_keep_existing_reservations(env):
    store, _, graph = env
    moving_case(store, graph)
    prior = deepcopy(store._robots["robot2"])
    reservations = tuple(r for r in store._traffic._reservations.snapshot() if r.robot_id == "robot2")
    row, selector = order(), Choosing()
    asyncio.run(service.coordinate_order_route(row, "11", "9", ["9", "7", "11"], graph=graph,
               source="EXISTING_SHORTEST_PATH", store=store, selector=selector))
    assert len(selector.calls) == 1
    assert len(selector.calls[0][1]) >= 2
    assert row["traffic_coordination"]["selected_by"] == "KEV"
    assert store._robots["robot2"] == prior
    assert tuple(r for r in store._traffic._reservations.snapshot() if r.robot_id == "robot2") == reservations


def test_calculated_policy_uses_same_candidates_without_calling_model(env, monkeypatch):
    store, _, graph = env
    moving_case(store, graph)
    prior = deepcopy(store._robots["robot2"])
    reservations = tuple(r for r in store._traffic._reservations.snapshot() if r.robot_id == "robot2")
    monkeypatch.setenv("FMS_TRAFFIC_SELECTION_POLICY", "MIN_EXPECTED_TIME")
    from backend.app.services.route_selector import kev_selector
    factory = Mock(side_effect=AssertionError("계산 방식에서 모델을 생성하면 안 됨"))
    monkeypatch.setattr(kev_selector, "KevSelector", factory)
    row = order()
    asyncio.run(service.coordinate_order_route(row, "11", "9", ["9", "7", "11"], graph=graph,
               source="EXISTING_SHORTEST_PATH", store=store))
    event = row["traffic_coordination"]
    assert len(event["candidates"]) >= 2
    assert event["selected_by"] == event["selection_policy"] == "MIN_EXPECTED_TIME"
    assert event["kev_calls"] == 0
    assert event["candidates"][event["choice"]]["estimated_metrics"]["new_request_completion_seconds"] == min(
        c["estimated_metrics"]["new_request_completion_seconds"] for c in event["candidates"].values())
    assert event["revalidated_before_apply"]
    assert store._robots["robot2"] == prior
    assert tuple(r for r in store._traffic._reservations.snapshot() if r.robot_id == "robot2") == reservations
    factory.assert_not_called()


def test_calculated_choice_uses_distance_only_when_times_tie():
    def candidate(seconds, distance):
        return dict(estimated_metrics=dict(new_request_completion_seconds=seconds, total_distance_m=distance))
    assert service._minimum_time_choice(dict(SHORT=candidate(5, 1), FAST=candidate(4, 2))) == "FAST"
    assert service._minimum_time_choice(dict(LONG=candidate(4, 2), SHORT=candidate(4, 1))) == "SHORT"


def test_unknown_selection_policy_rejects_before_movement(env, monkeypatch):
    store, _, graph = env
    monkeypatch.setenv("FMS_TRAFFIC_SELECTION_POLICY", "typo")
    with pytest.raises(ValueError, match="SELECTION_POLICY"):
        asyncio.run(service.coordinate_order_route(order(), "5", "0", ["0", "5"], graph=graph,
                   source="EXISTING_SHORTEST_PATH", store=store))
    assert all(r["route"] is None for r in store._robots.values())


@pytest.mark.parametrize("change", ["cancel", "map", "mode", "position", "invalid_choice"])
def test_stale_or_invalid_selection_never_launches_target(env, monkeypatch, change):
    store, mode, graph = env
    moving_case(store, graph)
    valid = [True]

    def mutate():
        if change == "cancel":
            valid[0] = False
        elif change == "mode":
            mode.mode = "real"
        elif change == "map":
            monkeypatch.setattr(service, "load_route_graph", lambda: {})
        elif change == "position":
            store.stop_robot("robot1")
            store._robots["robot1"]["occupied_node"] = "0"

    selector = Choosing(mutate, invalid=change == "invalid_choice")
    with pytest.raises(ValueError):
        asyncio.run(service.coordinate_order_route(order(), "11", "9", ["9", "7", "11"],
                   graph=graph, source="EXISTING_SHORTEST_PATH", valid=lambda: valid[0], selector=selector))
    assert store._robots["robot1"]["route"] is None
    assert not [r for r in store._traffic._reservations.snapshot() if r.robot_id == "robot1"]


def test_new_reservation_during_selection_is_revalidated(env, monkeypatch):
    store, _, graph = env
    moving_case(store, graph)
    monkeypatch.setenv("FMS_TRAFFIC_WAIT_SECONDS", ".1")
    added = []

    def mutate():
        chosen_node = "11"  # 모든 후보의 도착지를 다른 로봇이 예약
        row = Reservation(node_key(chosen_node), "robot3", "new-reservation", 0, monotonic(), math_inf)
        assert store._traffic._reservations.try_commit("robot3", "new-reservation", [row])
        added.append(row)

    math_inf = float("inf")
    with pytest.raises(ValueError, match="대기 한도"):
        asyncio.run(service.coordinate_order_route(order(), "11", "9", ["9", "7", "11"], graph=graph,
                   source="EXISTING_SHORTEST_PATH", selector=Choosing(mutate)))
    assert store._robots["robot1"]["route"] is None
    assert added[0] in store._traffic._reservations.snapshot()


def test_waiting_request_resumes_after_blocking_robot_leaves(env, monkeypatch):
    store, _, graph = env
    place(store, "robot1", "9", graph)
    place(store, "robot2", "7", graph)
    monkeypatch.setenv("FMS_TRAFFIC_REROUTE_LIMIT", "0")
    row = order()

    async def run():
        task = asyncio.create_task(service.coordinate_order_route(row, "11", "9", ["9", "7", "11"],
                                   graph=graph, source="EXISTING_SHORTEST_PATH"))
        await asyncio.sleep(.06)
        assert row["traffic_coordination"]["state"] == "WAITING_FOR_SAFE_ROUTE"
        assert store._robots["robot1"]["goal_node"] is None
        place(store, "robot2", "5", graph)
        await asyncio.wait_for(task, 3)

    asyncio.run(run())
    assert row["traffic_coordination"]["state"] == "APPLIED"


def test_task_cancellation_during_choice_does_not_commit(env):
    store, _, graph = env
    moving_case(store, graph)
    import threading
    entered, release = threading.Event(), threading.Event()

    class Blocking(Choosing):
        def select_choice(self, *args, **kwargs):
            entered.set()
            release.wait(3)
            return super().select_choice(*args, **kwargs)

    row = order()

    async def run():
        task = asyncio.create_task(service.coordinate_order_route(row, "11", "9", ["9", "7", "11"],
                                   graph=graph, source="EXISTING_SHORTEST_PATH", selector=Blocking()))
        while not entered.is_set():
            await asyncio.sleep(.01)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert row["traffic_coordination"]["state"] == "CANCELLED"
    assert store._robots["robot1"]["route"] is None


def test_real_and_algorithm_orders_keep_existing_flow(env, monkeypatch):
    _, _, _graph = env
    row = order()
    row["mode"] = "real"
    assert not service.enabled(row)
    row.update(mode="simulation", operation_mode="algorithm", driving_mode="standard")
    assert not service.enabled(row)
    row.update(operation_mode="ai")
    monkeypatch.setenv("FMS_ORDER_TRAFFIC_ENABLED", "0")
    assert not service.enabled(row)


def test_fourth_robot_occupancy_is_not_ignored(env):
    store, _, graph = env
    place(store, "robot1", "9", graph)
    fourth = deepcopy(store._robots["robot3"])
    x, y = DistanceAStar(graph).nodes["7"]
    fourth.update(robot_id="robot4", occupied_node="7", current_node="7", x=x, y=y)
    store._robots["robot4"] = fourth
    row = order()
    asyncio.run(order_route_dispatcher.dispatch_order_route(row, "11", "9"))
    assert "7" not in row["traffic_coordination"]["path"]
    assert store._robots["robot4"] == fourth


def test_choice_trace_preserves_actual_request_response_and_never_approves_execution(env):
    _, _, _graph = env

    class Traced:
        api_model = "kev-latest"
        last_inference = None

        def select_choice(self, state, candidates, instructions, *, question_id):
            self.last_inference = dict(request=dict(model=self.api_model, state=state,
                 questions={question_id: dict(type="choice", instructions=instructions, criteria=candidates)}),
                 response=dict(answers={question_id: dict(choice="B")}))
            return dict(choice="B")

    selector = Traced()
    result = service._select_choice_recorded(selector, dict(test=True), dict(A="10", B="5"),
                                            "Choose smaller.", dict(order_id="test", robot_id="robot1"))
    records = [json.loads(line) for line in (service.LOG_DIR / "selection-inferences.jsonl").read_text().splitlines()]
    assert result == dict(choice="B")
    assert [r["event"] for r in records] == ["REQUEST", "RESPONSE"]
    assert records[0]["request_id"] == records[1]["request_id"]
    assert records[1]["inference"]["request"] == selector.last_inference["request"]
    assert records[1]["exact_transport_request_saved"]
    assert not any(r["fms_execution_approved"] for r in records)


def test_choice_log_storage_failure_does_not_replace_or_fail_selection(env, monkeypatch, tmp_path):
    blocked = tmp_path / "regular-file"
    blocked.write_text("not a directory")
    monkeypatch.setattr(service, "LOG_DIR", blocked)
    selector = Choosing()
    result = service._select_choice_recorded(selector, {},
        dict(A=json.dumps(dict(estimated_metrics=dict(new_request_completion_seconds=10))),
             B=json.dumps(dict(estimated_metrics=dict(new_request_completion_seconds=5)))),
        "Choose smaller.", dict(order_id="test"))
    assert result["choice"] == "B"


def test_choice_error_is_logged_and_propagated_without_fallback(env):
    class Broken:
        def select_choice(self, *args, **kwargs):
            raise RuntimeError("test transport failure")

    with pytest.raises(RuntimeError, match="test transport"):
        service._select_choice_recorded(Broken(), {}, dict(A="1", B="2"), "Choose smaller.", {})
    records = [json.loads(line) for line in (service.LOG_DIR / "selection-inferences.jsonl").read_text().splitlines()]
    assert [r["event"] for r in records] == ["REQUEST", "ERROR"]


@pytest.mark.parametrize("same_pickup_pair", [False, True])
def test_normal_order_queue_uses_coordination_on_pickup_workstation_and_return(env, monkeypatch, same_pickup_pair):
    from backend.app.services import order_manager as orders_module
    from backend.app.services import simulation_order_runner as runner_module
    from backend.app.services import scenario_service, workstation_manager as station_module
    from backend.app.services import route_model_catalog

    store, mode, graph = env
    orders = orders_module.OrderManager()
    runner = runner_module.SimulationOrderRunner()
    runner.work_seconds = .15
    stations = station_module.WorkstationManager()
    scenario = scenario_service.ScenarioManager()
    monkeypatch.setattr(orders_module, "order_manager", orders)
    monkeypatch.setattr(orders_module, "mode_manager", mode)
    monkeypatch.setattr(orders_module, "scenario_manager", scenario)
    monkeypatch.setattr(orders_module, "workstation_manager", stations)
    monkeypatch.setattr(runner_module, "mode_manager", mode)
    monkeypatch.setattr(runner_module, "mock_fms", store)
    monkeypatch.setattr(runner_module, "simulation_order_runner", runner)
    monkeypatch.setattr(runner_module, "workstation_manager", stations)
    monkeypatch.setattr(station_module, "workstation_manager", stations)
    monkeypatch.setattr(route_model_catalog, "require_enabled_model", lambda *a: None)
    from backend.app.services.route_selector import kev_selector
    monkeypatch.setattr(kev_selector, "KevSelector", lambda **kw: Choosing())
    # 기존 이동기를 그대로 쓰되 시험 속도만 같은 비율로 올린다.
    store._traffic.speed_mps = 5.0
    original_advance = store.advance_mock_robot
    monkeypatch.setattr(mock_data, "SIMULATION_SPEED_MPS", 5.0)
    monkeypatch.setattr(store, "advance_mock_robot", lambda *a, **kw: original_advance(*a, speed_mps=5.0, **kw))
    applied = []
    original_record = service._record

    def record(row, event):
        if event["state"] == "APPLIED":
            applied.append((row["robot_id"], row["phase"],
                            {key: dict(status=value["status"], phase=value["phase"])
                             for key, value in orders.orders.items()},
                            {key: value["occupied_node"] for key, value in store._robots.items()}))
        return original_record(row, event)

    monkeypatch.setattr(service, "_record", record)

    async def run():
        created = orders.create_order(dict(A=1, B=0, C=0, D=0), 1, selector="kev", model="kev-08b")
        row = orders.orders[created["order_id"]]
        if same_pickup_pair:
            orders.create_order(dict(A=0, B=3, C=0, D=0), 3, selector="kev", model="kev-08b")
        prior = monotonic()
        try:
            while monotonic() - began < 12:
                await orders.tick()
                now = monotonic()
                store.advance_simulation(now-prior, now)
                from backend.app.services.occupancy import occupied_resource
                resources = [occupied_resource(robot, graph) for robot in store._robots.values()]
                assert len(resources) == len(set(resources))
                prior = now
                if any(o["status"] == "FAILED" for o in orders.orders.values()):
                    pytest.fail(str(orders.list_orders()))
                if all(o["status"] == o["phase"] == "COMPLETED" for o in orders.orders.values()):
                    break
                await asyncio.sleep(.01)
            assert row["status"] == row["phase"] == "COMPLETED", row
            phases = {r["phase"] for r in row["traffic_coordination_history"] if r["state"] == "APPLIED"}
            assert {"MOVING_TO_PICKUP", "MOVING_TO_WORKSTATION", "RETURNING"} <= phases
            if same_pickup_pair:
                second_start = next(item for item in applied if item[:2] == ("robot2", "MOVING_TO_PICKUP"))
                assert second_start[2][created["order_id"]]["status"] == "IN_PROGRESS"
                assert second_start[3]["robot1"] != "5"
                assert all(o["status"] == o["phase"] == "COMPLETED" for o in orders.orders.values())
        finally:
            await runner.shutdown()

    began = monotonic()
    asyncio.run(run())
