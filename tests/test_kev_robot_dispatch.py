"""KEV 배차와 dev 주문·도킹·MAPF 경계의 회귀 검사. 실제 서버/장비를 호출하지 않는다."""

import asyncio
import threading
from time import monotonic
from unittest.mock import Mock

import pytest

from backend.app.models.robot import Robot, RobotState
from backend.app.services import robot_dispatch_service as dispatch
from backend.app.services import order_manager as order_module
from backend.app.services.mock_data import mock_fms
from backend.app.services.mode_service import mode_manager, feature_engines
from backend.app.services.order_dispatcher import available_robots, battery_block_reason
from backend.app.services.simulation_order_runner import simulation_order_runner
from tests.test_charging_docking import docking, finish


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setenv("FMS_SIM_KEV_DISPATCH_ENABLED", "1")
    monkeypatch.setenv("FMS_REAL_KEV_DISPATCH_ENABLED", "0")
    monkeypatch.setenv("KEV_DISPATCH_TIMEOUT_SECONDS", "3")
    monkeypatch.setenv("KEV_DISPATCH_COOLDOWN_SECONDS", "30")
    from backend.app.services import route_model_catalog

    monkeypatch.setattr(route_model_catalog, "require_enabled_model", lambda *_: None)
    mock_fms.reset()
    mode_manager.set_mode("simulation", "ai")
    manager = order_module.OrderManager()
    started = []
    monkeypatch.setattr(simulation_order_runner, "start", started.append)
    # All initial candidates are near tied, unless a test overrides these graph costs.
    monkeypatch.setattr(dispatch, "plan_route", lambda start, target: {"cost": 1.0})
    yield manager, started
    manager._robot_dispatcher.close()
    mode_manager.set_mode("simulation", "algorithm")
    mock_fms.reset()


def order(manager, **extra):
    return manager.create_order(items={"A": 1, "B": 0, "C": 1, "D": 0}, total_quantity=2,
                                selector="kev", model="kev-08b", **extra)


async def answer(manager):
    await asyncio.wait_for(asyncio.shield(manager._robot_dispatcher._task), .5)
    await manager.tick()


def test_distance_filter_uses_first_pickup_and_012_window(context, monkeypatch):
    manager, _ = context
    public = order(manager)
    seen = []
    def distance(start, target):
        seen.append(target)
        return {"cost": {"0": 1, "1": 1.12, "2": 1.121}[start]}
    monkeypatch.setattr(dispatch, "plan_route", distance)
    request = dispatch.prepare_dispatch(manager.orders[public["order_id"]], ["robot1", "robot2", "robot3"], "simulation", {})
    assert request["candidates"] == ["robot1", "robot2"]
    assert seen == ["5", "5", "5"]  # A/C mixed order really visits N5 first.
    assert "pickup_eta_seconds" not in str(request["debug_record"])
    assert request["debug_record"]["state"]["dispatch_context"]["recent_workload_available"] is False


def test_closer_returning_robot_beats_farther_idle_without_kev(context, monkeypatch):
    manager, started = context
    mock_fms._robots["robot2"].update(status="NAVIGATING", goal_node="1", occupied_node="3", current_node="3")
    manager.returning["robot2"] = "old"
    monkeypatch.setattr(dispatch, "plan_route", lambda start, target: {"cost": .2 if start == "3" else 2})
    spy = Mock(side_effect=AssertionError("single candidate must not call KEV"))
    monkeypatch.setattr(dispatch, "request_choice", spy)
    public = order(manager)
    asyncio.run(manager.tick())
    assert manager.get_order(public["order_id"])["robot_id"] == "robot2"
    assert started == [public["order_id"]]
    spy.assert_not_called()


def test_kev_choice_does_not_change_ai_route_or_take_locks(context, monkeypatch):
    manager, started = context
    def choose(request, selector, **kwargs):
        assert manager.lock.acquire(timeout=.05)
        manager.lock.release()
        assert kwargs["timeout_seconds"] == 3
        return {"choice": "robot3"}, selector
    monkeypatch.setattr(dispatch, "request_choice", choose)
    async def run():
        public = order(manager)
        await manager.tick()
        assert public["order_id"] not in started
        await answer(manager)
        result = manager.get_order(public["order_id"])
        assert (result["robot_id"], result["driving_mode"], result["operation_mode"]) == ("robot3", "llm", "ai")
        assert feature_engines("ai") == {"route": "ai", "dispatch": "ai", "workstation": "ai"}
    asyncio.run(run())


@pytest.mark.parametrize("failure", [TimeoutError("timeout"), RuntimeError("server unavailable"), "robot99"])
def test_error_fallback_same_distance_group_and_30_second_cooldown(context, monkeypatch, failure):
    manager, _ = context
    clock = [100.0]
    monkeypatch.setattr(dispatch, "monotonic", lambda: clock[0])
    monkeypatch.setattr(dispatch, "plan_route", lambda start, target: {"cost": 3 if start == "0" else 1})
    calls = []
    def choose(request, selector, **kwargs):
        calls.append(request)
        if isinstance(failure, Exception):
            raise failure
        return {"choice": failure}, selector
    monkeypatch.setattr(dispatch, "request_choice", choose)
    async def run():
        public = order(manager)
        await manager.tick()
        await answer(manager)
        assert manager.get_order(public["order_id"])["robot_id"] == "robot2"
        assert manager._robot_dispatcher._retry_at["simulation"] == 130
        manager.orders.clear()
        mock_fms.reset()
        public2 = order(manager)
        clock[0] = 129.9
        await manager.tick()
        assert manager.get_order(public2["order_id"])["robot_id"] == "robot2"
        assert len(calls) == 1
        manager.orders.clear()
        mock_fms.reset()
        order(manager)
        clock[0] = 130
        await manager.tick()
        await asyncio.wait_for(manager._robot_dispatcher._task, .5)
        assert len(calls) == 2
    asyncio.run(run())


def test_slow_kev_never_blocks_order_tick_or_simulation(context, monkeypatch):
    manager, _ = context
    entered, release = threading.Event(), threading.Event()
    def choose(request, selector, **kwargs):
        entered.set()
        release.wait(1)
        return {"choice": "robot2"}, selector
    monkeypatch.setattr(dispatch, "request_choice", choose)
    async def run():
        order(manager)
        await asyncio.wait_for(manager.tick(), .1)
        for _ in range(20):
            if entered.is_set():
                break
            await asyncio.sleep(.002)
        assert entered.is_set()
        before = mock_fms._last_tick
        for _ in range(3):
            await asyncio.wait_for(manager.tick(), .1)
            mock_fms.advance_simulation(.1, monotonic())
        assert mock_fms._last_tick > before
        assert not manager._robot_dispatcher._task.done()
        release.set()
        await answer(manager)
    try:
        asyncio.run(run())
    finally:
        release.set()


def test_outer_timeout_covers_whole_call_and_late_result_is_ignored(context, monkeypatch):
    manager, _ = context
    monkeypatch.setenv("KEV_DISPATCH_TIMEOUT_SECONDS", ".02")
    release = threading.Event()
    def choose(request, selector, **kwargs):
        release.wait(.2)
        return {"choice": "robot3"}, selector
    monkeypatch.setattr(dispatch, "request_choice", choose)
    async def run():
        public = order(manager)
        await manager.tick()
        await answer(manager)
        assert manager.get_order(public["order_id"])["robot_id"] == "robot1"
        assert manager._robot_dispatcher._retry_at["simulation"] > monotonic()
        release.set()
        await asyncio.sleep(.01)
        assert manager.get_order(public["order_id"])["robot_id"] == "robot1"
    try:
        asyncio.run(run())
    finally:
        release.set()


@pytest.mark.parametrize("change", ["pause", "low_voltage", "unknown_voltage", "occupied_edge"])
def test_unsafe_candidate_cannot_receive_late_response(context, monkeypatch, change):
    manager, _ = context
    monkeypatch.setattr(dispatch, "request_choice", lambda request, selector, **kwargs: ({"choice": "robot2"}, selector))
    async def run():
        public = order(manager)
        await manager.tick()
        await manager._robot_dispatcher._task
        update = {"pause": {"status": "PAUSED"}, "low_voltage": {"battery_voltage": 11.1},
                  "unknown_voltage": {"battery_voltage": None}, "occupied_edge": {"occupied_edge": "edge"}}[change]
        mock_fms._robots["robot2"].update(update)
        await manager.tick()
        assert manager.get_order(public["order_id"])["robot_id"] is None
        assert manager._robot_dispatcher._request["candidates"] == ["robot1", "robot3"]
    asyncio.run(run())


def test_same_candidate_ids_with_changed_node_discards_result(context, monkeypatch):
    manager, _ = context
    monkeypatch.setattr(dispatch, "request_choice", lambda request, selector, **kwargs: ({"choice": "robot2"}, selector))
    async def run():
        public = order(manager)
        await manager.tick()
        await manager._robot_dispatcher._task
        mock_fms._robots["robot2"].update(current_node="4", occupied_node="4")
        await manager.tick()
        assert manager.get_order(public["order_id"])["robot_id"] is None
        assert manager._robot_dispatcher._request["candidates"] == ["robot1", "robot2", "robot3"]
    asyncio.run(run())


def test_cancel_does_not_assign_or_stop_any_robot(context, monkeypatch):
    manager, started = context
    monkeypatch.setattr(dispatch, "request_choice", lambda request, selector, **kwargs: ({"choice": "robot2"}, selector))
    async def run():
        public = order(manager)
        await manager.tick()
        await manager._robot_dispatcher._task
        assert manager.cancel_order(public["order_id"])
        await manager.tick()
        assert manager.get_order(public["order_id"])["status"] == "CANCELLED"
        assert not started
        assert all(not r["stop_requested"] for r in mock_fms.robot_snapshots("simulation"))
    asyncio.run(run())


def test_fifo_waits_for_first_response_and_never_double_assigns(context, monkeypatch):
    manager, started = context
    monkeypatch.setattr(dispatch, "request_choice", lambda request, selector, **kwargs: ({"choice": request["candidates"][-1]}, selector))
    async def run():
        orders = [order(manager) for _ in range(3)]
        await manager.tick()
        assert not started
        await answer(manager)
        assert started == [orders[0]["order_id"]]
        await answer(manager)
        # Last candidate is assigned directly; each robot gets at most one order.
        assert started == [o["order_id"] for o in orders]
        assert len({r["robot_id"] for r in manager.orders.values()}) == 3
    asyncio.run(run())


@pytest.mark.parametrize("real_enabled", ["0", "1"])
def test_environment_gate_preserves_ai_route(context, monkeypatch, real_enabled):
    monkeypatch.setenv("FMS_REAL_KEV_DISPATCH_ENABLED", real_enabled)
    assert feature_engines("ai", mode="real") == {
        "route": "ai", "dispatch": "ai" if real_enabled == "1" else "algorithm", "workstation": "ai"
    }
    assert feature_engines("algorithm", mode="simulation")["dispatch"] == "algorithm"


@pytest.mark.parametrize("voltage,reason", [(None, "battery_unknown"), (float('nan'), "battery_unknown"),
                                            (0, "battery_unknown"), (11.19, "low_battery"), (11.2, None), (12, None)])
def test_voltage_filter(voltage, reason):
    assert battery_block_reason(voltage) == reason


def test_voltage_history_minimum_expiry_disconnect_and_first_sample():
    robot = Robot("robot1")
    assert robot.min_battery_voltage(100) is None
    robot.update_battery(80, voltage=12, now=100)
    assert robot.min_battery_voltage(100) == 12  # No 60-second startup wait.
    robot.update_battery(80, voltage=11.1, now=110)
    robot.update_battery(80, voltage=12, now=120)
    assert robot.min_battery_voltage(170) == 11.1
    assert robot.min_battery_voltage(170.01) == 12
    assert robot.min_battery_voltage(181) is None
    robot.update_battery(80, voltage=12, now=182)
    robot.set_connected(False)
    assert robot.battery_voltage is None
    assert robot.min_battery_voltage(182) is None


def test_invalid_voltage_never_refreshes_history():
    robot = Robot("robot1")
    robot.update_battery(80, voltage=12, now=100)
    for voltage in (None, 0, -1, float('nan'), float('inf')):
        robot.update_battery(80, voltage=voltage, now=159)
    assert robot.min_battery_voltage(161) is None


def test_low_voltage_excludes_all_candidates_including_algorithm(context):
    for robot in mock_fms._robots.values():
        robot["battery_voltage"] = 11
    assert available_robots("simulation", set(), operation_mode="ai") == []
    assert available_robots("simulation", set(), operation_mode="algorithm") == []


@pytest.mark.parametrize("invalidate", [None, "contact_loss", "voltage_loss", "reconnect", "late_contact_loss"])
def test_real_charging_candidate_keeps_dock_safety_checks(context, docking, monkeypatch, invalidate):
    from backend.app.services import order_dispatcher, fleet_manager as fleet_module

    manager, _ = context
    finish(docking)
    monkeypatch.setenv("FMS_REAL_KEV_DISPATCH_ENABLED", "1")
    mode_manager.set_mode("real", "ai")
    monkeypatch.setattr(fleet_module, "fleet_manager", docking.fleet)
    monkeypatch.setattr(order_module, "fleet_manager", docking.fleet)
    monkeypatch.setattr(dispatch, "fleet_manager", docking.fleet)
    monkeypatch.setattr(order_module, "ros_gateway", docking.gateway)
    monkeypatch.setattr(order_dispatcher, "ros_gateway", docking.gateway)
    monkeypatch.setattr(order_dispatcher, "monotonic", lambda: docking.clock[0])
    for robot in docking.fleet.get_all_robots():
        robot.pose_received_at = docking.clock[0]
    idle = docking.fleet.get_robot("robot2")
    idle.goal_node = idle.route = None
    idle.set_state(RobotState.IDLE)
    # CHARGING robot1 is close; IDLE robot2 is deliberately far away.
    monkeypatch.setattr(dispatch, "plan_route", lambda start, target: {
        "cost": 1 if start == "0" or invalidate == "late_contact_loss" else 3})
    monkeypatch.setattr(dispatch, "request_choice", lambda request, selector, **kwargs: ({"choice": "robot1"}, selector))
    async def noop(*args):
        pass
    monkeypatch.setattr(manager, "_tick_real", noop)
    monkeypatch.setattr(manager, "_tick_real_returns", noop)
    if invalidate == "contact_loss":
        from tests.test_dock_mqtt import message
        docking.mqtt._on_message(None, None, message(1, 0))
    elif invalidate == "voltage_loss":
        docking.fleet.get_robot("robot1")._battery_voltage_samples.clear()
    elif invalidate == "reconnect":
        docking.mqtt._on_connect(Mock(), None, {}, 0)
    public = order(manager)
    async def run():
        await manager.tick()
        if invalidate == "late_contact_loss":
            from tests.test_dock_mqtt import message
            await manager._robot_dispatcher._task
            docking.mqtt._on_message(None, None, message(1, 0))
            await manager.tick()
    asyncio.run(run())
    # 도킹 성공이 확인된 뒤 센서 유실은 배차 제외 조건이 아니다. 전압 조건은 유지한다.
    expected = "robot2" if invalidate == "voltage_loss" else "robot1"
    assert manager.get_order(public["order_id"])["robot_id"] == expected
    if invalidate is None:
        # Selection does not start motion or bypass dock proof. Existing execution owns departure.
        assert docking.gateway._charging_docks["robot1"]["action_confirmed"]
        assert not docking.gateway.navigation_active("robot1")


def test_mode_round_trip_invalidates_old_response(context, monkeypatch):
    manager, _ = context
    monkeypatch.setattr(dispatch, "request_choice", lambda request, selector, **kwargs: ({"choice": "robot2"}, selector))
    async def run():
        public = order(manager)
        await manager.tick()
        await manager._robot_dispatcher._task
        mode_manager.set_mode("simulation", "algorithm")
        mode_manager.set_mode("simulation", "ai")
        await manager.tick()
        assert manager.get_order(public["order_id"])["robot_id"] is None
    asyncio.run(run())


def test_distance_failure_does_not_bypass_distance_policy(context, monkeypatch):
    manager, _ = context
    def unavailable(*args):
        raise ValueError("unreachable")
    monkeypatch.setattr(dispatch, "plan_route", unavailable)
    public = order(manager)
    asyncio.run(manager.tick())
    assert manager.get_order(public["order_id"])["status"] == "PENDING"
    assert manager._robot_dispatcher._task is None


def test_selected_robot_runs_pickups_workstation_return_through_existing_mapf_gate(context, monkeypatch):
    from backend.app.services import order_route_dispatcher
    from backend.app.services.workstation_manager import workstation_manager

    manager, _ = context
    monkeypatch.setattr(order_module, "order_manager", manager)
    monkeypatch.setenv("FMS_SIM_MAPF_ENABLED", "1")
    monkeypatch.setattr(simulation_order_runner, "start", type(simulation_order_runner).start.__get__(simulation_order_runner))
    monkeypatch.setattr(simulation_order_runner, "tasks", {})
    monkeypatch.setattr(simulation_order_runner, "work_seconds", .001)
    monkeypatch.setattr(workstation_manager, "_assigned", {})
    monkeypatch.setattr(dispatch, "request_choice", lambda request, selector, **kwargs: ({"choice": "robot3"}, selector))
    routes = []
    async def mapf_gate(row, target, start, valid=None):
        assert valid is None or valid()
        assert (row["operation_mode"], row["driving_mode"], row["selector"], row["model"]) == ("ai", "llm", "kev", "kev-08b")
        routes.append((row["robot_id"], target))
        with mock_fms._lock:
            mock_fms._robots[row["robot_id"]].update(
                occupied_node=target, current_node=target, occupied_edge=None,
                goal_node=None, route=None, status="IDLE", stop_requested=False)
        return {"status": "SUCCESS"}
    monkeypatch.setattr(order_route_dispatcher, "_dispatch_mapf", mapf_gate)
    async def run():
        public = order(manager)
        await manager.tick()
        await answer(manager)
        await asyncio.wait_for(asyncio.gather(*list(simulation_order_runner.tasks.values())), .5)
        assert manager.get_order(public["order_id"])["status"] == "COMPLETED"
        assert routes == [("robot3", "5"), ("robot3", "6"), ("robot3", "3"), ("robot3", "2")]
    asyncio.run(run())


def test_order_stress_scenario_does_not_disable_dispatch(context, monkeypatch):
    from backend.app.services.scenario_service import scenario_manager
    from backend.app.services.order_stress_test import STRESS_NAME

    manager, started = context
    with monkeypatch.context() as scenario_patch:
        scenario_patch.setattr(scenario_manager, "_status", {
            "state": "running", "name": STRESS_NAME, "run_id": "run", "robot_ids": []})
        public = order(manager, scenario_run_id="run")
        monkeypatch.setattr(dispatch, "plan_route", lambda start, target: {"cost": 0 if start == "0" else 3})
        asyncio.run(manager.tick())
        assert started == [public["order_id"]]


def test_graph_change_invalidates_response_even_if_candidate_ids_match(context, monkeypatch):
    manager, _ = context
    graph = {"revision": 1}
    monkeypatch.setattr(dispatch, "load_route_graph", lambda: graph)
    monkeypatch.setattr(dispatch, "request_choice", lambda request, selector, **kwargs: ({"choice": "robot2"}, selector))
    async def run():
        public = order(manager)
        await manager.tick()
        await manager._robot_dispatcher._task
        graph["revision"] = 2
        await manager.tick()
        assert manager.get_order(public["order_id"])["robot_id"] is None
    asyncio.run(run())


def test_ros_battery_callback_forwards_measured_voltage(monkeypatch):
    from types import SimpleNamespace
    ros_module = pytest.importorskip("backend.app.ros2.fms_ros_node")
    robot = Robot("robot1")
    monkeypatch.setattr(ros_module.fleet_manager, "get_robot", lambda rid: robot)
    ros_module.FmsRosNode._on_battery_state(None, "robot1", SimpleNamespace(percentage=80, voltage=11.1))
    assert robot.battery_voltage == 11.1
    assert battery_block_reason(robot.min_battery_voltage()) == "low_battery"


def test_discarded_http_requests_are_bounded_and_do_not_occupy_route_executor(context, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    manager, _ = context
    release = threading.Event()
    entered = threading.Event()
    concurrent = [0, 0]
    def choose(request, selector, **kwargs):
        concurrent[0] += 1
        concurrent[1] = max(concurrent)
        entered.set()
        release.wait(.5)
        concurrent[0] -= 1
        return {"choice": "robot2"}, selector
    monkeypatch.setattr(dispatch, "request_choice", choose)
    async def run():
        # Even a route/MAPF executor with only one thread stays available.
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        order(manager)
        await manager.tick()
        for _ in range(20):
            if entered.is_set():
                break
            await asyncio.sleep(.001)
        assert entered.is_set()
        for i in range(5):
            mock_fms._robots["robot2"].update(command_epoch=i+1)
            await manager.tick()
            await asyncio.sleep(.001)
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "route worker available"), .1) == "route worker available"
        release.set()
        await answer(manager)
        assert concurrent[1] == 1
    try:
        asyncio.run(run())
    finally:
        release.set()
