"""CHARGING 전용 직진 이탈과 일반 출발의 분리, 실제 도착·실행 종료를 검증한다."""

import math
from unittest.mock import Mock

import pytest
from action_msgs.msg import GoalStatus

from backend.app.models.robot import RobotState, NavigationType
from backend.app.services.station_exit import forward_distance
from tests.test_charging_docking import docking, finish, real_executor
from tests.test_dock_mqtt import message


@pytest.fixture
def exit_context(docking, monkeypatch):
    finish(docking)
    nav, sent, cancels, graph = real_executor(docking, monkeypatch)
    from backend.app.services.real_navigation import measured_distance
    from backend.app.ros2 import ros_gateway
    monkeypatch.setattr(ros_gateway, "measured_distance", measured_distance)
    docking.nav, docking.sent, docking.cancels = nav, sent, cancels
    return docking


def start_exit(ctx):
    ctx.nav.request("robot1", "3", NavigationType.GOAL, required_path=["0", "2", "3"])
    ctx.clock[0] += 1
    ctx.fleet.get_robot("robot1").pose_received_at = ctx.clock[0]
    ctx.nav.tick()
    ctx.node.send_station_exit.assert_called_once_with("robot1", pytest.approx(.2), .09, 20.)
    ctx.gateway._on_dock_navigation_started("robot1")


def release(ctx):
    ctx.clock[0] += .01
    ctx.mqtt._on_message(None, None, message(status=0))
    ctx.clock[0] += .31
    ctx.fleet.get_robot("robot1").pose_received_at = ctx.clock[0]


def test_charge_exit_waits_for_action_and_measured_front_node(exit_context):
    ctx = exit_context
    start_exit(ctx)
    robot = ctx.fleet.get_robot("robot1")
    assert robot.state == RobotState.UNDOCKING
    assert ctx.nav._traffic._protected_resources["robot1"] == {("node", "0")}
    assert not ctx.sent
    release(ctx)
    ctx.gateway._tick_charging_docks()
    assert not ctx.sent
    assert "robot1" in ctx.nav._preparing
    ctx.gateway.on_station_exit_result("robot1", GoalStatus.STATUS_SUCCEEDED)
    assert "robot1" in ctx.nav._preparing  # Action 성공만으로 좌표 도착을 가정하지 않는다.
    robot.x = 0.
    ctx.gateway._tick_charging_docks()
    assert not ctx.nav._preparing
    assert not ctx.gateway._departing_docks
    assert ctx.nav._traffic._required_paths["robot1"] == ("0", "2", "3")
    ctx.nav.tick()
    ctx.clock[0] += 1
    robot.pose_received_at = ctx.clock[0]
    ctx.nav.tick()
    assert ctx.sent
    assert ctx.sent[0]["waypoints"][0][:2] == (0, 1)


@pytest.mark.parametrize("sensor", ["on", "off", "disconnected", "reconnected"])
def test_exit_completes_from_action_and_pose_independently_of_sensor(exit_context, sensor):
    ctx = exit_context
    start_exit(ctx)
    robot = ctx.fleet.get_robot("robot1")
    if sensor == "off":
        release(ctx)
    elif sensor in ("disconnected", "reconnected"):
        ctx.mqtt._on_disconnect(None, None, 1)
        if sensor == "reconnected":
            ctx.mqtt._on_connect(Mock(), None, {}, 0)
    ctx.clock[0] += .1
    robot.update_pose(0., 0., 0.)
    ctx.gateway.on_station_exit_result("robot1", GoalStatus.STATUS_SUCCEEDED)
    assert not ctx.gateway._departing_docks
    assert not ctx.nav._preparing
    assert not ctx.cancels
    assert robot.state == RobotState.WAITING
    ctx.nav.tick()
    ctx.clock[0] += 1
    robot.pose_received_at = ctx.clock[0]
    ctx.nav.tick()
    assert ctx.sent


def test_exit_requires_pose_received_after_action_result(exit_context):
    ctx = exit_context
    start_exit(ctx)
    robot = ctx.fleet.get_robot("robot1")
    robot.update_pose(0., 0., 0.)
    ctx.clock[0] += .1
    ctx.gateway.on_station_exit_result("robot1", GoalStatus.STATUS_SUCCEEDED)
    assert "robot1" in ctx.nav._preparing
    assert not ctx.sent
    ctx.clock[0] += .1
    robot.update_pose(0., 0., 0.)
    ctx.gateway._tick_charging_docks()
    assert not ctx.nav._preparing


def test_consumed_on_cannot_recreate_charging_after_exit_or_normal_return(exit_context):
    ctx = exit_context
    start_exit(ctx)
    robot = ctx.fleet.get_robot("robot1")
    robot.update_pose(0., 0., 0.)
    ctx.gateway.on_station_exit_result("robot1", GoalStatus.STATUS_SUCCEEDED)
    ctx.nav.cancel_after_stop("robot1")
    robot.goal_node = robot.navigation_type = None
    robot.set_state(RobotState.IDLE)
    ctx.clock[0] += .5
    ctx.mqtt._on_message(None, None, message())  # 같은 ON의 반복 발행도 새 접촉은 아니다.
    ctx.gateway._tick_charging_docks()
    assert robot.state == RobotState.IDLE
    assert not ctx.gateway._charging_docks
    release(ctx)
    ctx.gateway._tick_charging_docks()
    ctx.clock[0] += .01
    ctx.mqtt._on_message(None, None, message())
    ctx.clock[0] += .31
    ctx.gateway._tick_charging_docks()
    assert robot.state == RobotState.CHARGING  # 새 OFF→ON 접촉은 다시 인식한다.


@pytest.mark.parametrize("state", [RobotState.TASK_ASSIGNED, RobotState.WAITING,
                                  RobotState.NAVIGATING, RobotState.UNDOCKING,
                                  RobotState.PAUSED, RobotState.EMERGENCY_STOP])
def test_contact_event_never_overwrites_active_work_or_stop(docking, state):
    ctx = docking
    robot = ctx.fleet.get_robot("robot1")
    robot.navigation_type = robot.goal_node = None
    if state == RobotState.PAUSED:
        robot.pause("navigation_aborted")
    else:
        robot.set_state(state)
    ctx.mqtt._on_message(None, None, message())
    ctx.clock[0] += .31
    ctx.gateway._tick_charging_docks()
    assert robot.state == state
    assert not ctx.gateway._charging_docks
    ctx.node.send_station_exit.assert_not_called()


@pytest.mark.parametrize("case", ["wrong_position", "late_result", "offline", "stale_pose"])
def test_exit_failure_never_releases_robot_into_order_route(exit_context, case):
    ctx = exit_context
    start_exit(ctx)
    robot = ctx.fleet.get_robot("robot1")
    release(ctx)
    if case != "wrong_position":
        robot.x = 0.
    if case == "offline":
        robot.set_connected(False)
    elif case == "stale_pose":
        robot.pose_received_at = ctx.clock[0] - 3
    elif case == "late_result":
        ctx.clock[0] += 21
    ctx.gateway.on_station_exit_result("robot1", GoalStatus.STATUS_SUCCEEDED)
    ctx.clock[0] += 11
    if case != "stale_pose":
        robot.pose_received_at = ctx.clock[0]
    ctx.gateway._tick_charging_docks()
    assert robot.state == RobotState.PAUSED
    assert not ctx.sent
    assert ctx.cancels
    assert "robot1" in ctx.nav._preparing  # 취소 수락이 아닌 terminal 확인까지 점유를 유지한다.
    assert ctx.nav._traffic._protected_resources["robot1"] == {("node", "0")}


@pytest.mark.parametrize("pose", [(-.2, 0, 0), (-.04, 0, math.pi), (0, 0, math.pi / 2)])
def test_normal_startup_uses_existing_route_without_straight_exit(docking, monkeypatch, pose):
    ctx = docking
    robot = ctx.fleet.get_robot("robot1")
    robot.navigation_type = robot.goal_node = None
    robot.update_pose(*pose)
    robot.set_state(RobotState.IDLE)
    ctx.mqtt._on_message(None, None, message(status=0))
    ctx.clock[0] += .31
    nav, sent, cancels, graph = real_executor(ctx, monkeypatch)
    ctx.gateway._tick_charging_docks()
    assert robot.state == RobotState.IDLE
    ctx.node.send_station_exit.assert_not_called()
    ctx.node.send_precision_dock.assert_not_called()
    nav.tick()
    assert not sent
    nav.request("robot1", "3", NavigationType.GOAL, required_path=["0", "2", "3"])
    ctx.clock[0] += 1
    robot.pose_received_at = ctx.clock[0]
    nav.tick()
    ctx.node.send_station_exit.assert_not_called()
    assert sent
    assert not nav._preparing
    assert not ctx.gateway._unverified_docks
    assert robot.state == RobotState.NAVIGATING


def test_unknown_or_retained_sensor_does_not_pause_idle_robot(docking):
    ctx = docking
    robot = ctx.fleet.get_robot("robot1")
    robot.navigation_type = robot.goal_node = None
    robot.set_state(RobotState.IDLE)
    ctx.gateway._tick_charging_docks()
    assert robot.state == RobotState.IDLE
    ctx.mqtt._on_message(None, None, message(status=0, retain=True))
    ctx.clock[0] += 1
    ctx.gateway._tick_charging_docks()
    assert robot.state == RobotState.IDLE
    ctx.mqtt._on_message(None, None, message(status=0))
    ctx.gateway._tick_charging_docks()
    assert robot.state == RobotState.IDLE
    ctx.clock[0] += .31
    ctx.gateway._tick_charging_docks()
    assert robot.state == RobotState.IDLE
    ctx.node.send_station_exit.assert_not_called()
    ctx.node.send_precision_dock.assert_not_called()


@pytest.mark.parametrize("robot_id,node,path,pose", [
    ("robot1", "0", ["0", "2", "3"], (-.04, 0, math.pi)),
    ("robot2", "1", ["1", "3"], (.96, 0, math.pi)),
    ("robot3", "2", ["2", "3"], (-.04, 1, math.pi)),
])
def test_each_front_node_allows_normal_order_without_sensor_or_straight_heading(
    docking, monkeypatch, robot_id, node, path, pose
):
    ctx = docking
    robot = ctx.fleet.get_robot(robot_id)
    robot.navigation_type = robot.goal_node = None
    robot.set_state(RobotState.IDLE)
    robot.update_pose(*pose)
    nav, sent, _, _ = real_executor(ctx, monkeypatch, robot_id)
    nav.refresh()
    ctx.gateway._tick_charging_docks()
    assert robot.occupied_node == node
    assert robot.state == RobotState.IDLE
    robot.assign_order("normal-order", {"A": 1}, 1, ["3"], None)
    nav.request(robot_id, "3", NavigationType.GOAL, required_path=path)
    ctx.clock[0] += 1
    robot.pose_received_at = ctx.clock[0]
    nav.tick()
    assert sent
    assert robot.order_id == "normal-order"
    ctx.node.send_station_exit.assert_not_called()
    assert not ctx.gateway._departing_docks


def test_live_contact_reconstructs_charging_without_docking_history(docking):
    robot = docking.fleet.get_robot("robot1")
    robot.navigation_type = robot.goal_node = None
    robot.set_state(RobotState.IDLE)
    docking.mqtt._on_message(None, None, message())
    docking.clock[0] += .31
    docking.gateway._tick_charging_docks()
    assert robot.state == RobotState.CHARGING
    assert docking.gateway.charging_dispatch_ready("robot1")
    assert docking.gateway.docking_station_snapshots()[0]["docking_complete"]
    docking.node.send_precision_dock.assert_not_called()
    docking.node.send_station_exit.assert_not_called()


@pytest.mark.parametrize("x,y,yaw", [(0.2, 0, 0), (-.2, 0, math.pi), (-.5, 0, 0)])
def test_wrong_start_geometry_does_not_start_straight_motion(exit_context, x, y, yaw):
    robot = exit_context.fleet.get_robot("robot1")
    robot.update_pose(x, y, yaw)
    robot.pose_received_at = exit_context.clock[0]
    with pytest.raises(ValueError):
        forward_distance(robot, (0, 0), exit_context.clock[0])
    exit_context.node.send_station_exit.assert_not_called()


@pytest.mark.parametrize("pose", [(-.2, -.08, 0), (-.2, 0, math.radians(20))])
def test_exit_does_not_reject_lateral_or_heading_thresholds(exit_context, pose):
    ctx = exit_context
    robot = ctx.fleet.get_robot("robot1")
    robot.update_pose(*pose)
    ctx.nav.request("robot1", "3", NavigationType.GOAL, required_path=["0", "2", "3"])
    ctx.clock[0] += 1
    robot.pose_received_at = ctx.clock[0]
    ctx.nav.tick()
    ctx.node.send_station_exit.assert_called_once()
    assert not ctx.sent
    assert robot.state == RobotState.UNDOCKING
    ctx.gateway.on_station_exit_result("robot1", GoalStatus.STATUS_SUCCEEDED)
    ctx.clock[0] += 1.01
    robot.pose_received_at = ctx.clock[0]
    ctx.gateway._tick_charging_docks()
    assert robot.state == RobotState.PAUSED  # 출발 문턱을 없애도 앞 노드 도착을 가정하지 않는다.
    assert not ctx.sent


def test_exit_action_cancel_is_held_until_terminal_and_ignores_stale_response():
    from threading import RLock
    from types import SimpleNamespace
    from backend.app.ros2.fms_ros_node import FmsRosNode
    entry = {"kind": "StationExit", "handle": None, "cancel_callback": Mock(), "cancel_sent": False}
    handle = Mock(accepted=True)
    future = Mock()
    future.result.return_value = handle
    node = SimpleNamespace(navigation_lock=RLock(), _auxiliary_goals={"robot1": entry},
                           station_exit_started_callback=Mock(), station_exit_result_callback=Mock(),
                           _finish_auxiliary=Mock(), _send_auxiliary_cancel=Mock(), _on_station_exit_result=Mock())
    FmsRosNode._on_station_exit_goal_response(node, "robot1", entry, future)
    assert node._auxiliary_goals["robot1"] is entry
    node._send_auxiliary_cancel.assert_called_once_with("robot1", entry)
    node._finish_auxiliary.assert_not_called()
    node._auxiliary_goals["robot1"] = {"kind": "new"}
    FmsRosNode._on_station_exit_result(node, "robot1", entry, future)
    node._finish_auxiliary.assert_not_called()


def test_emergency_stop_waits_for_straight_action_terminal_before_releasing_state(exit_context):
    ctx = exit_context
    start_exit(ctx)
    ctx.nav._cancel_goal = ctx.gateway._cancel_navigation_goal
    active = [True]
    terminal_callbacks = []
    ctx.node.has_active_auxiliary.side_effect = lambda rid: active[0]
    ctx.node.cancel_auxiliary.side_effect = lambda rid, callback=None: terminal_callbacks.append(callback)
    ctx.node.cancel_follow_waypoints.side_effect = lambda rid, callback: callback()
    result = ctx.gateway.emergency_stop("robot1")
    assert not result["cancel_confirmed"]
    assert "robot1" in ctx.nav._stopping
    assert "robot1" in ctx.nav._preparing
    assert "robot1" in ctx.nav._traffic._protected_resources
    assert not ctx.sent
    active[0] = False
    terminal_callbacks[-1]()
    assert not ctx.nav._preparing
    assert not ctx.nav._requests
    assert ctx.fleet.get_robot("robot1").state == RobotState.EMERGENCY_STOP
    ctx.gateway.on_station_exit_result("robot1", GoalStatus.STATUS_SUCCEEDED)
    assert not ctx.sent


def test_contact_based_charging_exits_after_order_changes_state(docking, monkeypatch):
    ctx = docking
    robot = ctx.fleet.get_robot("robot1")
    robot.navigation_type = robot.goal_node = None
    robot.set_state(RobotState.IDLE)
    robot.x, robot.y = -.2, 0.
    ctx.mqtt._on_message(None, None, message())
    ctx.clock[0] += .31
    ctx.gateway._tick_charging_docks()
    assert robot.state == RobotState.CHARGING
    robot.assign_order("first-order", {"A": 1}, 1, ["3"], None)
    assert robot.state == RobotState.TASK_ASSIGNED
    nav, sent, cancels, _ = real_executor(ctx, monkeypatch)
    nav.request("robot1", "3", NavigationType.GOAL, required_path=["0", "2", "3"])
    ctx.clock[0] += 1
    robot.pose_received_at = ctx.clock[0]
    nav.tick()
    ctx.node.send_station_exit.assert_called_once()
    assert robot.order_id == "first-order"
    ctx.node.send_precision_dock.assert_not_called()
    assert not sent


def test_drive_on_heading_nonzero_result_error_cannot_be_success():
    from threading import RLock
    from types import SimpleNamespace
    from backend.app.ros2.fms_ros_node import FmsRosNode
    entry = {"kind": "StationExit"}
    node = SimpleNamespace(navigation_lock=RLock(), _auxiliary_goals={"robot1": entry},
                           station_exit_result_callback=Mock(), _finish_auxiliary=Mock())
    future = Mock()
    future.result.return_value = SimpleNamespace(status=GoalStatus.STATUS_SUCCEEDED,
                                                 result=SimpleNamespace(error_code=723))
    FmsRosNode._on_station_exit_result(node, "robot1", entry, future)
    node._finish_auxiliary.assert_called_once_with("robot1", entry, GoalStatus.STATUS_ABORTED,
                                                  node.station_exit_result_callback)


def test_other_robot_cannot_enter_front_node_while_departure_is_waiting_or_moving(exit_context, monkeypatch):
    from backend.app.ros2 import ros_gateway
    from tests.test_fixed_route_reservation import graph
    from backend.app.services.pathfinding import DistanceAStar
    ctx = exit_context
    nodes = DistanceAStar(graph()).nodes
    monkeypatch.setattr(ros_gateway, "get_node", lambda n: {"id": n, "x": nodes[n][0], "y": nodes[n][1]})
    other = ctx.fleet.register_robot("robot2")
    other.update_pose(1, 0, 0)
    other.pose_received_at = ctx.clock[0]
    other.current_node = other.occupied_node = "1"
    other.set_state(RobotState.IDLE)
    ctx.mqtt._on_message(None, None, message(2, 0))
    ctx.clock[0] += .31
    for robot in ctx.fleet.get_all_robots():
        robot.pose_received_at = ctx.clock[0]
    ctx.nav.request("robot1", "3", NavigationType.GOAL, required_path=["0", "2", "3"])
    ctx.nav.refresh()  # API 조회가 이탈 전 노드 보호를 해제하면 안 된다.
    ctx.nav.request("robot2", "0", NavigationType.GOAL, required_path=["1", "0"])
    for _ in range(3):
        ctx.clock[0] += 1
        for robot in ctx.fleet.get_all_robots():
            robot.pose_received_at = ctx.clock[0]
        ctx.nav.tick()
    assert not ctx.sent
    assert ctx.node.send_station_exit.call_count == 1
    assert ctx.node.send_station_exit.call_args.args[0] == "robot1"
    assert ctx.nav._robots["robot2"]["occupied_node"] == "1"
