"""실기체 없이 충전 도킹 문맥·주문 복귀·출발 안전 경로를 검증한다."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from action_msgs.msg import GoalStatus

from backend.app.models.robot import NavigationType, Robot, RobotState
from backend.app.ros2 import ros_gateway as gateway_module
from backend.app.services import mqtt_manager as mqtt_module
from backend.app.services.fleet_manager import FleetManager
from tests.test_dock_mqtt import message


@pytest.fixture
def docking(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(gateway_module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mqtt_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mqtt_module, "DOCK_DEBOUNCE_S", 0.3)
    monkeypatch.setattr(mqtt_module, "DOCK_SENSOR_MAX_AGE_S", 0)
    monkeypatch.setattr(gateway_module, "DOCK_STATION_MAP", {"robot1": 1, "robot2": 2, "robot3": 3})
    manager = mqtt_module.MQTTManager()
    manager._on_connect(Mock(), None, {}, 0)
    fleet = FleetManager()
    for i in (1, 2, 3):
        robot = fleet.register_robot(f"robot{i}")
        robot.update_pose(0, 0, 0)
        robot.update_battery(80, voltage=12)
        robot.goal_node = robot.occupied_node = robot.current_node = str(i - 1)
        robot.navigation_type = NavigationType.CHARGING
        robot.set_state(RobotState.NAVIGATING)
    monkeypatch.setattr(gateway_module, "mqtt_manager", manager)
    monkeypatch.setattr(gateway_module, "fleet_manager", fleet)
    monkeypatch.setattr(gateway_module, "get_node", lambda node: {"id": node, "x": 0, "y": 0})
    monkeypatch.setattr(gateway_module, "measured_distance", lambda *args: 0)
    gateway = gateway_module.RosGateway()
    node = Mock()
    node.has_active_auxiliary.return_value = False
    gateway.set_ros_node(node)
    return SimpleNamespace(gateway=gateway, fleet=fleet, mqtt=manager, clock=clock, node=node)


def begin(ctx, robot_id="robot1"):
    ctx.gateway.on_navigation_result(robot_id, GoalStatus.STATUS_SUCCEEDED)
    assert ctx.fleet.get_robot(robot_id).state == RobotState.DOCKING


def finish(ctx, robot_id="robot1", station=1):
    begin(ctx, robot_id)
    ctx.clock[0] += 0.01
    ctx.mqtt._on_message(None, None, message(station))
    ctx.gateway.on_precision_dock_result(robot_id, GoalStatus.STATUS_SUCCEEDED)
    ctx.clock[0] += 0.31
    ctx.gateway._tick_charging_docks()
    assert ctx.fleet.get_robot(robot_id).state == RobotState.CHARGING


@pytest.mark.parametrize("contact_first", [True, False])
def test_action_success_and_contact_in_either_order(docking, contact_first):
    ctx = docking
    begin(ctx)
    if contact_first:
        ctx.clock[0] += 0.01
        ctx.mqtt._on_message(None, None, message())
        ctx.clock[0] += 0.31
    ctx.gateway.on_precision_dock_result("robot1", GoalStatus.STATUS_SUCCEEDED)
    if not contact_first:
        assert ctx.fleet.get_robot("robot1").state == RobotState.DOCKING
        ctx.clock[0] += 0.01
        ctx.mqtt._on_message(None, None, message())
        ctx.clock[0] += 0.31
        ctx.gateway._tick_charging_docks()
    assert ctx.fleet.get_robot("robot1").state == RobotState.CHARGING
    assert ctx.gateway.docking_station_snapshots()[0]["docking_complete"]


@pytest.mark.parametrize("status", [GoalStatus.STATUS_ABORTED, GoalStatus.STATUS_CANCELED])
def test_action_failure_cannot_be_overridden_by_contact(docking, status):
    begin(docking)
    docking.mqtt._on_message(None, None, message())
    docking.clock[0] += 1
    docking.gateway.on_precision_dock_result("robot1", status)
    docking.gateway._tick_charging_docks()
    robot = docking.fleet.get_robot("robot1")
    assert robot.state == RobotState.PAUSED
    assert robot.pause_reason_code == "precision_dock_failed"


@pytest.mark.parametrize("case", ["wrong_station", "old_contact", "retained", "manual"])
def test_unrelated_contacts_do_not_complete_docking(docking, case):
    ctx = docking
    if case == "old_contact":
        ctx.mqtt._on_message(None, None, message())
        ctx.clock[0] += 1
    if case != "manual":
        begin(ctx)
        ctx.gateway.on_precision_dock_result("robot1", GoalStatus.STATUS_SUCCEEDED)
    else:
        ctx.fleet.get_robot("robot1").set_state(RobotState.IDLE)
    ctx.clock[0] += 0.01
    ctx.mqtt._on_message(None, None, message(2 if case == "wrong_station" else 1, retain=case == "retained"))
    ctx.clock[0] += 0.31
    ctx.gateway._tick_charging_docks()
    assert ctx.fleet.get_robot("robot1").state != RobotState.CHARGING


def test_missing_mapping_prevents_precision_dock(docking, monkeypatch):
    monkeypatch.setattr(gateway_module, "DOCK_STATION_MAP", {})
    docking.gateway.on_navigation_result("robot1", GoalStatus.STATUS_SUCCEEDED)
    assert docking.fleet.get_robot("robot1").pause_reason_code == "dock_station_unverified"
    docking.node.send_precision_dock.assert_not_called()


@pytest.mark.parametrize("succeeded", [True, False])
def test_contact_or_action_timeout_pauses_without_false_success(docking, succeeded):
    begin(docking)
    if succeeded:
        docking.gateway.on_precision_dock_result("robot1", GoalStatus.STATUS_SUCCEEDED)
    docking.node.has_active_auxiliary.return_value = not succeeded
    docking.clock[0] += 100
    docking.gateway._tick_charging_docks()
    robot = docking.fleet.get_robot("robot1")
    assert robot.state == RobotState.PAUSED
    assert robot.pause_reason_code == ("dock_contact_timeout" if succeeded else "dock_action_timeout")
    if not succeeded:
        docking.node.cancel_auxiliary.assert_called_once_with("robot1")


@pytest.mark.parametrize("interruption", ["cancel", "pause", "estop", "offline", "mqtt_reconnect"])
def test_interrupted_attempt_ignores_late_events(docking, interruption, monkeypatch):
    ctx = docking
    begin(ctx)
    robot = ctx.fleet.get_robot("robot1")
    if interruption == "cancel":
        monkeypatch.setattr(ctx.gateway._navigation, "stop", lambda *args, **kwargs: None)
        ctx.gateway._stop_before_navigation("robot1", lambda: None, "new_goal_request")
    elif interruption == "pause":
        robot.pause("precision_dock_failed")
    elif interruption == "estop":
        robot.set_state(RobotState.EMERGENCY_STOP)
    elif interruption == "offline":
        robot.set_connected(False)
    else:
        ctx.mqtt._on_disconnect(None, None, 1)
        ctx.mqtt._on_connect(Mock(), None, {}, 0)
    ctx.mqtt._on_message(None, None, message())
    ctx.clock[0] += 0.31
    ctx.gateway.on_precision_dock_result("robot1", GoalStatus.STATUS_SUCCEEDED)
    ctx.gateway._tick_charging_docks()
    assert robot.state != RobotState.CHARGING


def test_multiple_robots_and_stations_are_independent(docking):
    finish(docking)
    begin(docking, "robot2")
    docking.gateway.on_precision_dock_result("robot2", GoalStatus.STATUS_SUCCEEDED)
    docking.clock[0] += 0.01
    docking.mqtt._on_message(None, None, message(3))
    docking.clock[0] += 0.31
    docking.gateway._tick_charging_docks()
    assert docking.fleet.get_robot("robot1").state == RobotState.CHARGING
    assert docking.fleet.get_robot("robot2").state == RobotState.DOCKING
    docking.mqtt._on_message(None, None, message(2))
    docking.clock[0] += 0.31
    docking.gateway._tick_charging_docks()
    assert docking.fleet.get_robot("robot2").state == RobotState.CHARGING


def test_workstation_aruco_does_not_require_charging_sensor(docking):
    robot = docking.fleet.get_robot("robot1")
    robot.navigation_type = NavigationType.GOAL
    robot.goal_node = "5"
    docking.gateway.on_navigation_result("robot1", GoalStatus.STATUS_SUCCEEDED)
    docking.node.send_aruco_align.assert_called_once_with(robot_id="robot1", marker_id=24)
    docking.gateway.on_precision_dock_result("robot1", GoalStatus.STATUS_SUCCEEDED)
    assert robot.state == RobotState.DOCKING
    docking.gateway.on_aruco_align_result("robot1", GoalStatus.STATUS_SUCCEEDED)
    assert robot.state == RobotState.IDLE


def test_charging_zero_velocity_keeps_state_and_nonzero_is_rejected(docking):
    finish(docking)
    docking.gateway.cmd_vel("robot1", 0, 0)
    assert docking.fleet.get_robot("robot1").state == RobotState.CHARGING
    with pytest.raises(ValueError):
        docking.gateway.cmd_vel("robot1", 0.1, 0)


def test_already_docked_charging_command_does_not_redock(docking):
    finish(docking)
    result = docking.gateway.navigate_to_charging_station("robot1")
    assert result["status"] == "SUCCESS"
    assert docking.node.send_precision_dock.call_count == 1
    assert docking.fleet.get_robot("robot1").state == RobotState.CHARGING


def test_idle_robots_are_dispatched_before_charging_then_returning(docking, monkeypatch):
    from backend.app.services import order_dispatcher

    finish(docking)
    robot2 = docking.fleet.get_robot("robot2")
    robot2.set_state(RobotState.IDLE)
    robot2.goal_node = robot2.route = None
    # 배차의 기존 monotonic/pose 조건도 유지한다.
    monkeypatch.setattr(order_dispatcher, "monotonic", lambda: docking.clock[0])
    monkeypatch.setattr(order_dispatcher, "ros_gateway", docking.gateway)
    from backend.app.services.fleet_manager import fleet_manager
    monkeypatch.setattr(fleet_manager, "get_all_robots", docking.fleet.get_all_robots)
    assert order_dispatcher.available_robots("real", set()) == ["robot2", "robot1"]
    assert order_dispatcher.available_robots("real", {"robot2"}) == ["robot1"]
    docking.fleet.get_robot("robot1").battery = None
    assert order_dispatcher.available_robots("real", {"robot2"}) == []


def test_new_order_uses_existing_stop_and_reserved_navigation(docking, monkeypatch):
    finish(docking)
    robot = docking.fleet.get_robot("robot1")
    robot.assign_order("new-order", {"A": 1}, 1, ["5"], None)
    stopped = []
    def stop(robot_id, callback, **kwargs):
        stopped.append(robot_id)
        callback()
    monkeypatch.setattr(docking.gateway._navigation, "stop", stop)
    request = Mock()
    monkeypatch.setattr(docking.gateway._navigation, "request", request)
    docking.gateway.navigate_to_node("robot1", "5", _order_step=True, required_path=["0", "5"])
    assert stopped == ["robot1"]
    request.assert_called_once_with("robot1", "5", NavigationType.GOAL, required_path=("0", "5"))
    assert robot.order_id == "new-order"
    # request is mocked: real admission transfers the proof via on_requested.
    assert "robot1" in docking.gateway._charging_docks


def test_departure_monitors_release_without_starting_motion_from_sensor(docking, monkeypatch):
    finish(docking)
    docking.gateway._on_dock_navigation_requested("robot1")
    docking.gateway._on_dock_navigation_started("robot1")
    robot = docking.fleet.get_robot("robot1")
    robot.set_state(RobotState.NAVIGATING)
    stop = Mock()
    monkeypatch.setattr(docking.gateway._navigation, "stop", stop)
    docking.gateway._tick_charging_docks()
    docking.clock[0] += 11
    docking.gateway._tick_charging_docks()
    assert robot.pause_reason_code == "dock_release_timeout"
    stop.assert_called_once()


def test_station_api_hides_real_sensors_in_simulation(docking, monkeypatch):
    from backend.app.routers import robots
    monkeypatch.setattr(robots, "mode_manager", SimpleNamespace(mode="simulation"))
    assert asyncio.run(robots.fetch_docking_stations()) == []
    monkeypatch.setattr(robots, "mode_manager", SimpleNamespace(mode="real"))
    monkeypatch.setattr(gateway_module, "ros_gateway", docking.gateway)
    rows = asyncio.run(robots.fetch_docking_stations())
    assert [row["station"] for row in rows] == [1, 2, 3]
    assert [row["robot_id"] for row in rows] == ["robot1", "robot2", "robot3"]


def test_reconnected_docked_robot_requires_operator_review(docking):
    finish(docking)
    robot = docking.fleet.get_robot("robot1")
    robot.set_connected(False)
    robot.set_connected(True)
    assert robot.state == RobotState.PAUSED
    assert robot.pause_reason_code == "reconnected_with_active_work"


@pytest.mark.parametrize("loss", ["release", "mqtt_disconnect"])
def test_charging_contact_loss_never_starts_motion(docking, loss):
    finish(docking)
    if loss == "release":
        docking.mqtt._on_message(None, None, message(status=0))
    else:
        docking.mqtt._on_disconnect(None, None, 1)
    docking.gateway._tick_charging_docks()
    assert docking.fleet.get_robot("robot1").state == RobotState.PAUSED
    docking.node.send_follow_waypoints_goal.assert_not_called()


def test_verified_release_clears_departure_monitor(docking):
    finish(docking)
    docking.gateway._on_dock_navigation_requested("robot1")
    docking.gateway._on_dock_navigation_started("robot1")
    docking.fleet.get_robot("robot1").set_state(RobotState.NAVIGATING)
    docking.mqtt._on_message(None, None, message(status=0))
    docking.gateway._tick_charging_docks()
    assert not docking.gateway._departing_docks


def test_return_is_not_completed_at_idle_station_or_before_contact(docking, monkeypatch):
    from backend.app.services import order_manager as order_module
    manager = order_module.OrderManager()
    monkeypatch.setattr(order_module, "fleet_manager", docking.fleet)
    monkeypatch.setattr(order_module, "ros_gateway", docking.gateway)
    monkeypatch.setattr(docking.gateway, "navigation_active", lambda _: False)
    row = {"order_id": "done", "status": "COMPLETED", "phase": "RETURNING"}
    manager.orders["done"] = row
    manager.returning["robot1"] = "done"
    manager._return_started.add("robot1")
    robot = docking.fleet.get_robot("robot1")
    robot.set_state(RobotState.IDLE)
    asyncio.run(manager._tick_real_returns())
    assert row["phase"] == "RETURNING"
    robot.set_state(RobotState.NAVIGATING)
    finish(docking)
    asyncio.run(manager._tick_real_returns())
    assert row["phase"] == "COMPLETED"
    assert not manager.returning


def test_return_route_keeps_required_path_and_explicit_charging_purpose(docking, monkeypatch):
    from backend.app.services import order_route_dispatcher as dispatcher
    from backend.app.services.fleet_manager import fleet_manager
    monkeypatch.setattr(dispatcher, "mode_manager", SimpleNamespace(mode="real"))
    monkeypatch.setattr(gateway_module, "ros_gateway", docking.gateway)
    monkeypatch.setattr(fleet_manager, "get_robot", docking.fleet.get_robot)
    monkeypatch.setattr(dispatcher, "load_route_graph", lambda: {})
    # Algorithm 경로를 선택한 자동 복귀가 동일 주행 API에 충전 목적만 추가한다.
    navigate = Mock()
    monkeypatch.setattr(docking.gateway, "navigate_to_node", navigate)
    order = {"mode": "real", "robot_id": "robot1", "operation_mode": "algorithm", "driving_mode": "standard"}
    asyncio.run(dispatcher.dispatch_order_route(order, "0", "0", charging_return=True))
    navigate.assert_called_once_with("robot1", "0", _order_step=True, required_path=None, _charging_return=True)


def test_order_retry_preference_cannot_override_idle_priority(docking, monkeypatch):
    from backend.app.services import order_manager as order_module
    manager = order_module.OrderManager()
    monkeypatch.setattr(order_module, "fleet_manager", docking.fleet)
    monkeypatch.setattr(order_module, "mode_manager", SimpleNamespace(mode="real", operation_mode="algorithm"))
    monkeypatch.setattr(order_module, "available_robots", lambda *args: ["robot2", "robot1"])
    finish(docking)
    docking.fleet.get_robot("robot2").set_state(RobotState.IDLE)
    row = manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
    manager.orders[row["order_id"]]["last_robot_id"] = "robot2"
    async def no_move(_row):
        pass
    monkeypatch.setattr(manager, "_tick_real", no_move)
    asyncio.run(manager.tick())
    assert manager.orders[row["order_id"]]["robot_id"] == "robot2"


@pytest.mark.parametrize("action_status,success,expected", [
    (GoalStatus.STATUS_SUCCEEDED, True, GoalStatus.STATUS_SUCCEEDED),
    (GoalStatus.STATUS_SUCCEEDED, False, GoalStatus.STATUS_ABORTED),
    (GoalStatus.STATUS_CANCELED, True, GoalStatus.STATUS_CANCELED),
])
def test_precision_dock_checks_action_result_success(action_status, success, expected):
    from threading import RLock
    FmsRosNode = pytest.importorskip("backend.app.ros2.fms_ros_node").FmsRosNode
    entry = {"kind": "PrecisionDock"}
    node = SimpleNamespace(
        navigation_lock=RLock(), _auxiliary_goals={"robot1": entry},
        _finish_auxiliary=Mock(), precision_dock_result_callback=Mock(),
    )
    future = Mock()
    future.result.return_value = SimpleNamespace(status=action_status, result=SimpleNamespace(success=success))
    FmsRosNode._on_precision_dock_result(node, "robot1", future, entry)
    node._finish_auxiliary.assert_called_once_with("robot1", entry, expected, node.precision_dock_result_callback)


def test_previous_precision_dock_action_cannot_finish_new_attempt():
    from threading import RLock
    FmsRosNode = pytest.importorskip("backend.app.ros2.fms_ros_node").FmsRosNode
    node = SimpleNamespace(navigation_lock=RLock(), _auxiliary_goals={"robot1": {}}, _finish_auxiliary=Mock())
    future = Mock()
    FmsRosNode._on_precision_dock_result(node, "robot1", future, {"kind": "old-goal"})
    future.result.assert_not_called()
    node._finish_auxiliary.assert_not_called()


def test_charging_departure_really_reserves_the_selected_route(docking, monkeypatch):
    from backend.app.services import real_navigation
    from backend.app.services.real_navigation import RealNavigation
    from backend.app.services.reservation import ReservationTable
    from tests.test_fixed_route_reservation import graph

    finish(docking)
    robot = docking.fleet.get_robot("robot1")
    robot.pose_received_at = docking.clock[0]
    docking.fleet._robots = {"robot1": robot}
    nav = RealNavigation(
        docking.fleet, Mock(), lambda _rid, callback: callback(), Mock(), {}, 0.15,
        reservations=ReservationTable(), clock=lambda: docking.clock[0], station_nodes={"robot1": "0"}, mapf_ready=lambda _: True,
        before_request=docking.gateway._validate_dock_departure,
        on_requested=docking.gateway._on_dock_navigation_requested,
        before_send=docking.gateway._before_dock_navigation_send,
        departure_ready=docking.gateway._dock_planning_ready,
    )
    docking.gateway._navigation = nav
    monkeypatch.setattr(real_navigation, "load_route_graph", graph)
    nav._stopping["robot1"] = None
    with pytest.raises(ValueError):
        nav.request("robot1", "3", NavigationType.GOAL, required_path=["0", "2", "3"])
    assert not nav._traffic._reservations.snapshot()
    assert robot.state == RobotState.CHARGING
    nav._stopping.clear()
    docking.gateway.navigate_to_node("robot1", "3", _order_step=True, required_path=["0", "2", "3"])
    assert nav._robots["robot1"]["route"]["node_ids"] == ["0", "2", "3"]
    assert nav._traffic._reservations.snapshot()
    assert robot.state == RobotState.WAITING
    assert robot.docked_station is None


@pytest.mark.parametrize("delay", [0, 20, 120])
def test_departure_timeout_starts_only_at_nav2_acceptance(docking, delay):
    finish(docking)
    gateway = docking.gateway
    gateway._on_dock_navigation_requested("robot1")
    robot = docking.fleet.get_robot("robot1")
    robot.set_state(RobotState.WAITING)
    docking.clock[0] += delay
    gateway._tick_charging_docks()
    assert robot.state == RobotState.WAITING
    assert gateway._departing_docks["robot1"]["moving_at"] is None
    gateway._on_dock_navigation_started("robot1")
    docking.clock[0] += 9.9
    gateway._tick_charging_docks()
    assert robot.state == RobotState.WAITING
    docking.clock[0] += .2
    gateway._navigation.stop = Mock()
    gateway._tick_charging_docks()
    assert robot.pause_reason_code == "dock_release_timeout"
    gateway._navigation.stop.assert_called_once()


def test_release_before_acceptance_or_repeated_level_is_not_departure_proof(docking):
    finish(docking)
    gateway = docking.gateway
    gateway._on_dock_navigation_requested("robot1")
    docking.mqtt._on_message(None, None, message(status=0))
    docking.clock[0] += 1
    gateway._on_dock_navigation_started("robot1")
    docking.clock[0] += 1
    docking.mqtt._on_message(None, None, message(status=0))
    gateway._tick_charging_docks()
    assert "robot1" in gateway._departing_docks


@pytest.mark.parametrize("state", [RobotState.IDLE, RobotState.TASK_ASSIGNED])
def test_restart_unknown_station_robot_cannot_start_order(docking, state):
    robot = docking.fleet.get_robot("robot1")
    robot.navigation_type = None
    robot.goal_node = None
    robot.set_state(state)
    docking.gateway._tick_charging_docks()
    assert robot.pause_reason_code == "dock_restart_unverified"
    with pytest.raises(ValueError):
        docking.gateway._validate_dock_departure("robot1")
    with pytest.raises(ValueError):
        docking.gateway.cmd_vel("robot1", .1, 0)
    docking.node.send_follow_waypoints_goal.assert_not_called()


@pytest.mark.parametrize("battery", [None, 0, -1, 101, float('nan'), float('inf')])
def test_invalid_battery_blocks_charging_dispatch(docking, battery):
    finish(docking)
    docking.fleet.get_robot("robot1").battery = battery
    assert not docking.gateway.charging_dispatch_ready("robot1")


def test_contact_lost_then_reconnect_does_not_restore_automatic_departure(docking):
    finish(docking)
    docking.mqtt._on_message(None, None, message(status=0))
    docking.gateway._tick_charging_docks()
    robot = docking.fleet.get_robot("robot1")
    robot.set_connected(False)
    robot.set_connected(True)
    with pytest.raises(ValueError):
        docking.gateway._validate_dock_departure("robot1")


def real_executor(ctx, monkeypatch):
    from backend.app.services import real_navigation
    from backend.app.services.reservation import ReservationTable
    from tests.test_fixed_route_reservation import graph
    robot = ctx.fleet.get_robot("robot1")
    robot.pose_received_at = ctx.clock[0]
    ctx.fleet._robots = {"robot1": robot}
    sent, cancels = [], []
    nav = real_navigation.RealNavigation(
        ctx.fleet, lambda **kw: sent.append(kw),
        lambda rid, callback: cancels.append((rid, callback)), Mock(), {}, .15,
        reservations=ReservationTable(), clock=lambda: ctx.clock[0],
        station_nodes={"robot1": "0"}, mapf_ready=lambda _: True,
        before_send=ctx.gateway._before_dock_navigation_send,
        departure_ready=ctx.gateway._dock_planning_ready,
        before_request=ctx.gateway._validate_dock_departure,
        on_requested=ctx.gateway._on_dock_navigation_requested,
        navigation_kind=ctx.gateway._dock_navigation_kind,
    )
    ctx.gateway._navigation = nav
    monkeypatch.setattr(real_navigation, "load_route_graph", graph)
    monkeypatch.setattr("backend.app.services.route_graph.load_route_graph", graph)
    return nav, sent, cancels, graph()


def test_charging_atomic_mapf_admission_validates_proof_and_reserves(docking, monkeypatch):
    from backend.app.services.mode_service import mode_manager
    finish(docking)
    nav, sent, cancels, graph = real_executor(docking, monkeypatch)
    monkeypatch.setattr(mode_manager, "_mode", "real")
    nav.refresh()
    assert nav.robot_snapshot("robot1")["status"] == "IDLE"
    pending = {"robot1": {"node": "3"}}
    response = nav.mapf.commit_routes(graph, pending, {"robot1": ["0", "2", "3"]}, {"robot1": []}, ["robot1"], now=docking.clock[0])
    assert response
    assert nav._traffic._reservations.snapshot()
    assert not sent
    assert "robot1" in docking.gateway._departing_docks
    assert nav._robots["robot1"]["route"]["node_ids"] == ["0", "2", "3"]


def test_charging_atomic_mapf_rejects_reconnected_sensor(docking, monkeypatch):
    from backend.app.services.mode_service import mode_manager
    finish(docking)
    nav, sent, cancels, graph = real_executor(docking, monkeypatch)
    monkeypatch.setattr(mode_manager, "_mode", "real")
    nav.refresh()
    docking.mqtt._on_disconnect(None, None, 1)
    docking.mqtt._on_connect(Mock(), None, {}, 0)
    with pytest.raises(ValueError):
        nav.mapf.commit_routes(graph, {"robot1": {"node": "3"}}, {"robot1": ["0", "2", "3"]}, {"robot1": []}, ["robot1"], now=docking.clock[0])
    assert not sent
    assert not nav._traffic._reservations.snapshot()


def test_sensor_failure_during_departure_preserves_reservation_until_terminal(docking, monkeypatch):
    finish(docking)
    nav, sent, cancels, graph = real_executor(docking, monkeypatch)
    nav.request("robot1", "3", NavigationType.GOAL, required_path=["0", "2", "3"])
    docking.clock[0] += 1
    docking.fleet.get_robot("robot1").pose_received_at = docking.clock[0]
    nav.tick()
    assert sent
    docking.gateway._on_dock_navigation_started("robot1")
    before = nav._traffic._reservations.snapshot()
    docking.mqtt._on_disconnect(None, None, 1)
    docking.gateway._tick_charging_docks()
    assert nav._traffic._reservations.snapshot() == before
    assert "robot1" in nav._executing
    assert cancels
    cancels[-1][1]()
    assert "robot1" not in nav._requests
    assert "robot1" not in nav._executing
    assert nav._robots["robot1"]["occupied_node"] == "0"


def test_sensor_failure_while_waiting_prevents_goal_send(docking, monkeypatch):
    finish(docking)
    nav, sent, cancels, graph = real_executor(docking, monkeypatch)
    nav.request("robot1", "3", NavigationType.GOAL, required_path=["0", "2", "3"])
    before = nav._traffic._reservations.snapshot()
    docking.mqtt._on_disconnect(None, None, 1)
    docking.clock[0] += 1
    docking.fleet.get_robot("robot1").pose_received_at = docking.clock[0]
    nav.tick()
    assert not sent
    assert cancels
    assert nav._traffic._reservations.snapshot() == before


def test_failed_departure_can_redock_only_after_stop_confirmation(docking, monkeypatch):
    finish(docking)
    nav, sent, cancels, graph = real_executor(docking, monkeypatch)
    nav.request("robot1", "3", NavigationType.GOAL, required_path=["0", "2", "3"])
    docking.clock[0] += 1
    docking.fleet.get_robot("robot1").pose_received_at = docking.clock[0]
    nav.tick()
    docking.gateway._on_dock_navigation_started("robot1")
    docking.clock[0] += 10.1
    robot = docking.fleet.get_robot("robot1")
    robot.pose_received_at = docking.clock[0]
    docking.gateway._tick_charging_docks()
    docking.gateway.navigate_to_charging_station("robot1")
    # New return replaces the after-stop callback, but cannot send before terminal.
    assert len(sent) == 1
    cancels[-1][1]()
    assert nav._requests["robot1"][1] == NavigationType.CHARGING


def test_mapf_return_preserves_charging_purpose(docking, monkeypatch):
    from backend.app.services.order_manager import order_manager
    from backend.app.services.mode_service import mode_manager
    robot = docking.fleet.get_robot("robot1")
    robot.navigation_type = robot.goal_node = None
    robot.occupied_node = "3"
    robot.set_state(RobotState.IDLE)
    docking.gateway._dock_startup_checked.add("robot1")
    nav, sent, cancels, graph = real_executor(docking, monkeypatch)
    robot.x, robot.y = 2., 0.
    monkeypatch.setattr(order_manager, "returning", {"robot1": "old-order"})
    monkeypatch.setattr(mode_manager, "_mode", "real")
    graph["features"].append({"geometry": {"type": "MultiLineString"},
                              "properties": {"id": "reverse", "startid": "3", "endid": "1", "weight": 1}})
    monkeypatch.setattr("backend.app.services.real_navigation.load_route_graph", lambda: graph)
    response = nav.mapf.commit_routes(graph, {"robot1": {"node": "0"}}, {"robot1": ["3", "1", "0"]}, {"robot1": []}, ["robot1"], now=docking.clock[0])
    assert response
    assert nav._requests["robot1"][1] == NavigationType.CHARGING
    assert docking.fleet.get_robot("robot1").navigation_type == NavigationType.CHARGING
    assert not sent


def test_short_departure_waits_for_release_before_aruco_or_omx(docking):
    finish(docking)
    gateway = docking.gateway
    gateway._on_dock_navigation_requested("robot1")
    gateway._on_dock_navigation_started("robot1")
    robot = docking.fleet.get_robot("robot1")
    robot.goal_node = "5"
    robot.navigation_type = NavigationType.GOAL
    gateway.on_navigation_result("robot1", GoalStatus.STATUS_SUCCEEDED)
    docking.node.send_aruco_align.assert_not_called()
    assert robot.state == RobotState.DOCKING
    docking.clock[0] += 1
    docking.mqtt._on_message(None, None, message(status=0))
    gateway._tick_charging_docks()
    docking.node.send_aruco_align.assert_called_once_with(robot_id="robot1", marker_id=24)


def test_arrival_waiting_for_release_times_out_without_followup(docking, monkeypatch):
    finish(docking)
    gateway = docking.gateway
    gateway._on_dock_navigation_requested("robot1")
    gateway._on_dock_navigation_started("robot1")
    robot = docking.fleet.get_robot("robot1")
    robot.goal_node = "5"
    robot.navigation_type = NavigationType.GOAL
    gateway.on_navigation_result("robot1", GoalStatus.STATUS_SUCCEEDED)
    monkeypatch.setattr(gateway._navigation, "stop", Mock())
    docking.clock[0] += 11
    gateway._tick_charging_docks()
    assert robot.pause_reason_code == "dock_release_timeout"
    docking.node.send_aruco_align.assert_not_called()
    gateway._navigation.stop.assert_called_once()


def test_three_charging_robots_use_atomic_reservations_and_redock(docking, monkeypatch):
    from backend.app.services import real_navigation
    from backend.app.services.reservation import ReservationTable
    from backend.app.services.pathfinding import DistanceAStar
    from backend.app.services.mode_service import mode_manager
    from tests.test_real_kev_mapf import graph as fleet_graph
    for i in (1, 2, 3):
        finish(docking, f"robot{i}", i)
    graph = fleet_graph()
    # Three independent station-to-work routes, with explicit return edges.
    graph["features"] = [f for f in graph["features"] if f["geometry"]["type"] == "Point"]
    for i in range(3):
        for a, b in ((str(i), str(i + 3)), (str(i + 3), str(i))):
            graph["features"].append({"geometry": {"type": "MultiLineString"},
                                      "properties": {"id": f"{a}-{b}", "startid": a, "endid": b, "weight": 6}})
    nodes = DistanceAStar(graph).nodes
    for i, robot in enumerate(docking.fleet.get_all_robots()):
        robot.x, robot.y = nodes[str(i)]
        robot.pose_received_at = docking.clock[0]
    sent = []
    nav = real_navigation.RealNavigation(
        docking.fleet, lambda **kw: sent.append(kw), lambda rid, callback: callback(),
        lambda rid: docking.gateway.on_navigation_result(rid, GoalStatus.STATUS_SUCCEEDED), {}, .15,
        reservations=ReservationTable(), clock=lambda: docking.clock[0],
        station_nodes=gateway_module.CHARGING_STATION_NODES, mapf_ready=lambda _: True,
        before_send=docking.gateway._before_dock_navigation_send,
        departure_ready=docking.gateway._dock_planning_ready,
        before_request=docking.gateway._validate_dock_departure,
        on_requested=docking.gateway._on_dock_navigation_requested,
    )
    docking.gateway._navigation = nav
    monkeypatch.setattr(real_navigation, "load_route_graph", lambda: graph)
    monkeypatch.setattr(mode_manager, "_mode", "real")
    pending = {f"robot{i+1}": {"node": str(i+3)} for i in range(3)}
    paths = {f"robot{i+1}": [str(i), str(i+3)] for i in range(3)}
    response = nav.mapf.commit_routes(graph, pending, paths, {rid: [] for rid in pending}, list(pending), now=docking.clock[0])
    assert set(response) == set(pending)
    docking.clock[0] += 1
    for robot in docking.fleet.get_all_robots():
        robot.pose_received_at = docking.clock[0]
    nav.tick()
    assert {row["robot_id"] for row in sent} == set(pending)
    for i in range(3):
        docking.gateway._on_dock_navigation_started(f"robot{i+1}")
    docking.clock[0] += .01
    for i in range(3):
        docking.mqtt._on_message(None, None, message(i+1, 0))
    docking.gateway._tick_charging_docks()
    assert not docking.gateway._departing_docks
    for i in range(3):
        rid = f"robot{i+1}"
        robot = docking.fleet.get_robot(rid)
        robot.x, robot.y = nodes[str(i+3)]
        robot.pose_received_at = docking.clock[0]
        nav.on_result(rid, True)
        docking.gateway.on_aruco_align_result(rid, GoalStatus.STATUS_SUCCEEDED)
        assert robot.state == RobotState.IDLE
    for i in range(3):
        nav.request(f"robot{i+1}", str(i), NavigationType.CHARGING, required_path=[str(i+3), str(i)])
    docking.clock[0] += 1
    for robot in docking.fleet.get_all_robots():
        robot.pose_received_at = docking.clock[0]
    nav.tick()
    assert len(sent) == 6
    for i in range(3):
        rid = f"robot{i+1}"
        robot = docking.fleet.get_robot(rid)
        robot.x, robot.y = nodes[str(i)]
        robot.pose_received_at = docking.clock[0]
        nav.on_result(rid, True)
        assert robot.state == RobotState.DOCKING
        docking.clock[0] += .01
        docking.mqtt._on_message(None, None, message(i+1))
        docking.gateway.on_precision_dock_result(rid, GoalStatus.STATUS_SUCCEEDED)
    docking.clock[0] += .31
    docking.gateway._tick_charging_docks()
    assert all(r.state == RobotState.CHARGING for r in docking.fleet.get_all_robots())


def test_failed_action_keeps_departure_block_even_after_emergency_release(docking):
    begin(docking)
    docking.gateway.on_precision_dock_result("robot1", GoalStatus.STATUS_ABORTED)
    robot = docking.fleet.get_robot("robot1")
    robot.set_state(RobotState.EMERGENCY_STOP)
    result = docking.gateway.emergency_release("robot1")
    assert result["state"] == "PAUSED"
    assert robot.pause_reason_code == "dock_restart_unverified"
    with pytest.raises(ValueError):
        docking.gateway._validate_dock_departure("robot1")


def test_real_dispatch_acquires_navigation_lock_before_order_lock(docking, monkeypatch):
    from backend.app.services import order_manager as order_module
    from contextlib import contextmanager
    from threading import RLock
    manager = order_module.OrderManager()
    order_lock = RLock()
    manager.lock = order_lock
    @contextmanager
    def checked_navigation_lock():
        # A real non-reentrant second thread must be able to acquire the order
        # lock when tick first enters the navigation lock.
        from concurrent.futures import ThreadPoolExecutor
        def try_order():
            acquired = order_lock.acquire(timeout=.1)
            if acquired:
                order_lock.release()
            return acquired
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(try_order).result(timeout=1)
        yield
    monkeypatch.setattr(order_module, "ros_gateway", SimpleNamespace(navigation=SimpleNamespace(lock=checked_navigation_lock())))
    monkeypatch.setattr(order_module, "mode_manager", SimpleNamespace(mode="real", operation_mode="algorithm"))
    monkeypatch.setattr(order_module, "available_robots", lambda *args: [])
    asyncio.run(manager.tick())
