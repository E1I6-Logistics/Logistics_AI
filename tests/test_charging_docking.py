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
        robot.update_battery(80)
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
    assert "robot1" in docking.gateway._departing_docks


def test_departure_monitors_release_without_starting_motion_from_sensor(docking, monkeypatch):
    finish(docking)
    docking.gateway._departing_docks["robot1"] = docking.gateway._charging_docks.pop("robot1")
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
    assert asyncio.run(robots.fetch_docking_stations()) == {"mode": "simulation", "stations": []}
    monkeypatch.setattr(robots, "mode_manager", SimpleNamespace(mode="real"))
    monkeypatch.setattr(gateway_module, "ros_gateway", docking.gateway)
    rows = asyncio.run(robots.fetch_docking_stations())["stations"]
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
    docking.gateway._departing_docks["robot1"] = docking.gateway._charging_docks.pop("robot1")
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
        reservations=ReservationTable(), clock=lambda: docking.clock[0], station_nodes={"robot1": "0"},
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
