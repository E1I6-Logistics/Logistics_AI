"""CHARGING에서의 수동 속도 명령과 자동 이탈의 실행 경계를 검증한다."""

import pytest
from action_msgs.msg import GoalStatus

from backend.app.models.robot import NavigationType, RobotState
from tests.test_charging_docking import docking, finish
from tests.test_dock_mqtt import message
from tests.test_station_exit import exit_context, start_exit


@pytest.mark.parametrize("linear,angular", [(.2, 0), (-.2, 0), (0, .8), (0, -.8)])
def test_manual_charging_motion_and_release_with_contact_on(docking, linear, angular):
    ctx = docking
    finish(ctx)
    robot = ctx.fleet.get_robot("robot1")
    robot.update_battery(36, voltage=11.14)  # 배차 전압 필터는 수동 조작에 적용하지 않는다.
    contact = dict(ctx.gateway._charging_docks["robot1"])
    for _ in range(3):
        ctx.gateway.cmd_vel("robot1", linear, angular)
        ctx.gateway._tick_charging_docks()
        assert robot.state == RobotState.MOVING
    ctx.gateway.cmd_vel("robot1", 0, 0)
    ctx.node.publish_cmd_vel.assert_called_with(robot_id="robot1", linear_x=0, angular_z=0)
    assert robot.state == RobotState.CHARGING
    assert ctx.gateway._charging_docks["robot1"] == contact
    assert not ctx.gateway._departing_docks
    ctx.node.send_station_exit.assert_not_called()
    ctx.node.send_follow_waypoints_goal.assert_not_called()


@pytest.mark.parametrize("off_before_stop", [True, False])
def test_manual_exit_becomes_idle_after_contact_release(docking, off_before_stop):
    ctx = docking
    finish(ctx)
    robot = ctx.fleet.get_robot("robot1")
    ctx.gateway.cmd_vel("robot1", .2, 0)
    if not off_before_stop:
        ctx.gateway.cmd_vel("robot1", 0, 0)
        assert robot.state == RobotState.CHARGING
    ctx.mqtt._on_message(None, None, message(status=0))
    ctx.clock[0] += .31
    ctx.gateway._tick_charging_docks()
    if off_before_stop:
        assert robot.state == RobotState.MOVING
        ctx.gateway.cmd_vel("robot1", 0, 0)
    assert robot.state == RobotState.IDLE
    assert robot.docked_station is None
    assert "robot1" not in ctx.gateway._charging_docks
    assert "robot1" not in ctx.gateway._departing_docks


def test_manual_stop_with_contact_on_keeps_automatic_departure(exit_context):
    ctx = exit_context
    ctx.gateway.cmd_vel("robot1", .09, 0)
    ctx.gateway.cmd_vel("robot1", 0, 0)
    assert ctx.fleet.get_robot("robot1").state == RobotState.CHARGING
    start_exit(ctx)  # 기존 앞 노드 이탈 Action을 그대로 거친다.


def test_wasd_cannot_overlap_running_station_exit(exit_context):
    ctx = exit_context
    start_exit(ctx)
    ctx.node.publish_cmd_vel.reset_mock()
    with pytest.raises(ValueError, match="자동 동작 중"):
        ctx.gateway.cmd_vel("robot1", .2, 0)
    ctx.node.publish_cmd_vel.assert_not_called()
    ctx.gateway.cmd_vel("robot1", 0, 0)
    assert ctx.fleet.get_robot("robot1").state == RobotState.UNDOCKING
    assert "robot1" in ctx.gateway._departing_docks


@pytest.mark.parametrize("activity", ["auxiliary", "navigation_request", "cancel_wait", "assigned_order"])
def test_charging_manual_command_waits_for_automatic_work_to_end(docking, activity):
    ctx = docking
    finish(ctx)
    robot = ctx.fleet.get_robot("robot1")
    if activity == "auxiliary":
        ctx.node.has_active_auxiliary.return_value = True
    elif activity == "navigation_request":
        ctx.gateway.navigation._requests["robot1"] = ("3", NavigationType.GOAL)
    elif activity == "cancel_wait":
        ctx.gateway.navigation._stopping["robot1"] = None
    else:
        robot.assign_order("new-order", {"A": 1}, 1, ["3"], None)
    state = robot.state
    with pytest.raises(ValueError, match="자동 동작 중"):
        ctx.gateway.cmd_vel("robot1", .2, 0)
    ctx.node.publish_cmd_vel.assert_not_called()
    ctx.gateway.cmd_vel("robot1", 0, 0)
    assert robot.state == state


@pytest.mark.parametrize("blocked", ["emergency_stop", "dock_failure", "offline"])
def test_manual_charging_does_not_bypass_stop_or_failure(docking, blocked):
    ctx = docking
    finish(ctx)
    robot = ctx.fleet.get_robot("robot1")
    if blocked == "emergency_stop":
        robot.set_state(RobotState.EMERGENCY_STOP)
    elif blocked == "dock_failure":
        ctx.gateway._unverified_docks.add("robot1")
        robot.pause("station_exit_failed")
    else:
        robot.set_connected(False)
    with pytest.raises(ValueError):
        ctx.gateway.cmd_vel("robot1", .2, 0)
    ctx.node.publish_cmd_vel.assert_not_called()


def test_failed_manual_publish_preserves_charging_context(docking):
    ctx = docking
    finish(ctx)
    ctx.node.publish_cmd_vel.side_effect = RuntimeError("publisher unavailable")
    with pytest.raises(RuntimeError, match="publisher unavailable"):
        ctx.gateway.cmd_vel("robot1", .2, 0)
    assert ctx.fleet.get_robot("robot1").state == RobotState.CHARGING
    assert "robot1" in ctx.gateway._charging_docks


def failed_exit_with_pending_terminal(ctx):
    start_exit(ctx)
    ctx.nav._cancel_goal = ctx.gateway._cancel_navigation_goal
    active = [True]
    auxiliary_callbacks, navigation_callbacks = [], []
    ctx.node.has_active_auxiliary.side_effect = lambda rid: active[0]
    ctx.node.cancel_auxiliary.side_effect = lambda rid, callback=None: auxiliary_callbacks.append(callback)
    ctx.node.cancel_follow_waypoints.side_effect = lambda rid, callback: navigation_callbacks.append(callback)
    ctx.gateway.on_station_exit_result("robot1", GoalStatus.STATUS_ABORTED)
    return active, auxiliary_callbacks, navigation_callbacks


def test_manual_recovery_waits_for_all_terminal_results_and_keeps_order_paused(exit_context, monkeypatch):
    import asyncio
    from backend.app.services import order_manager as orders

    ctx = exit_context
    active, auxiliary_callbacks, navigation_callbacks = failed_exit_with_pending_terminal(ctx)
    robot = ctx.fleet.get_robot("robot1")
    robot.order_id = "paused-order"
    ctx.node.publish_cmd_vel.reset_mock()
    for stage in ("auxiliary_pending", "navigation_pending"):
        with pytest.raises(ValueError):
            ctx.gateway.cmd_vel("robot1", .2, 0)
        ctx.node.publish_cmd_vel.assert_not_called()
        assert "robot1" not in ctx.gateway._station_exit_manual_ready
        if stage == "auxiliary_pending":
            active[0] = False
            auxiliary_callbacks[-1]()
    navigation_callbacks[-1]()
    assert "robot1" in ctx.gateway._station_exit_manual_ready
    for linear in (.2, .2, 0):
        ctx.gateway.cmd_vel("robot1", linear, 0)
        ctx.gateway.advance_navigation()
        assert robot.state == RobotState.PAUSED
    assert robot.order_id == "paused-order"
    assert robot.goal_node is robot.route is robot.navigation_type is None
    ctx.node.publish_cmd_vel.assert_called_with(robot_id="robot1", linear_x=0, angular_z=0)
    assert not ctx.sent
    monkeypatch.setattr(orders, "fleet_manager", ctx.fleet)
    manager = orders.OrderManager()
    row = dict(order_id="paused-order", robot_id="robot1", phase="MOVING_TO_PICKUP", route_started=False)
    asyncio.run(manager._tick_real(row))
    assert not row["route_started"]
    assert not manager._route_tasks


def test_preflight_exit_failure_also_allows_manual_recovery_after_stop(exit_context):
    import math

    ctx = exit_context
    robot = ctx.fleet.get_robot("robot1")
    robot.update_pose(-.2, 0, math.pi)
    callbacks = []
    ctx.nav._cancel_goal = ctx.gateway._cancel_navigation_goal
    ctx.node.cancel_follow_waypoints.side_effect = lambda rid, callback: callbacks.append(callback)
    ctx.nav.request("robot1", "3", NavigationType.GOAL, required_path=["0", "2", "3"])
    ctx.clock[0] += 1
    robot.pose_received_at = ctx.clock[0]
    ctx.nav.tick()
    assert robot.pause_reason_code == "station_exit_failed"
    ctx.node.send_station_exit.assert_not_called()
    with pytest.raises(ValueError):
        ctx.gateway.cmd_vel("robot1", .2, 0)
    callbacks[-1]()
    ctx.gateway.cmd_vel("robot1", .2, 0)
    ctx.gateway.cmd_vel("robot1", 0, 0)
    assert robot.state == RobotState.PAUSED
    assert not ctx.sent


def test_manual_recovery_is_not_unlocked_by_cancellation_timeout(exit_context):
    ctx = exit_context
    failed_exit_with_pending_terminal(ctx)
    ctx.clock[0] += 6
    ctx.nav.tick()
    assert ctx.fleet.get_robot("robot1").pause_reason_code == "cancel_timeout"
    assert "robot1" not in ctx.gateway._station_exit_manual_ready
    with pytest.raises(ValueError):
        ctx.gateway.cmd_vel("robot1", .2, 0)


def test_late_terminal_after_timeout_can_unlock_manual_recovery(exit_context):
    ctx = exit_context
    active, auxiliary_callbacks, navigation_callbacks = failed_exit_with_pending_terminal(ctx)
    ctx.clock[0] += 6
    robot = ctx.fleet.get_robot("robot1")
    robot.update_pose(-.2, 0, 0)
    ctx.nav.tick()
    active[0] = False
    auxiliary_callbacks[-1]()
    navigation_callbacks[-1]()
    ctx.gateway.cmd_vel("robot1", .2, 0)
    assert robot.state == RobotState.PAUSED
    assert not ctx.nav._requests
    assert not ctx.sent


def test_manual_recovery_preserves_protection_when_cleanup_cannot_finish(exit_context, monkeypatch):
    from backend.app.services import real_navigation

    ctx = exit_context
    active, auxiliary_callbacks, navigation_callbacks = failed_exit_with_pending_terminal(ctx)
    def unavailable_graph():
        raise OSError("graph unavailable")
    monkeypatch.setattr(real_navigation, "load_route_graph", unavailable_graph)
    active[0] = False
    auxiliary_callbacks[-1]()
    navigation_callbacks[-1]()
    ctx.node.publish_cmd_vel.reset_mock()
    protected = dict(ctx.nav._traffic._protected_resources)
    reservations = ctx.nav._traffic._reservations.snapshot()
    ctx.gateway.cmd_vel("robot1", .2, 0)
    assert "robot1" in ctx.nav._cancelled
    assert ctx.fleet.get_robot("robot1").state == RobotState.PAUSED
    assert ctx.nav._traffic._protected_resources == protected
    assert ctx.nav._traffic._reservations.snapshot() == reservations
    assert not ctx.sent
    ctx.node.publish_cmd_vel.assert_called_with(robot_id="robot1", linear_x=.2, angular_z=0)


def test_paused_manual_recovery_can_emergency_stop_but_cannot_move_while_stopped(exit_context):
    ctx = exit_context
    active, auxiliary_callbacks, navigation_callbacks = failed_exit_with_pending_terminal(ctx)
    active[0] = False
    auxiliary_callbacks[-1]()
    navigation_callbacks[-1]()
    ctx.gateway.cmd_vel("robot1", .2, 0)
    ctx.gateway.emergency_stop("robot1")
    assert ctx.fleet.get_robot("robot1").state == RobotState.EMERGENCY_STOP
    ctx.node.publish_cmd_vel.assert_called_with(robot_id="robot1", linear_x=0, angular_z=0)
    with pytest.raises(ValueError, match="비상정지"):
        ctx.gateway.cmd_vel("robot1", .2, 0)


def test_failure_terminal_during_emergency_stop_requires_release_before_manual_recovery(exit_context):
    ctx = exit_context
    active, auxiliary_callbacks, navigation_callbacks = failed_exit_with_pending_terminal(ctx)
    failed_exit_callback = auxiliary_callbacks[-1]
    ctx.gateway.emergency_stop("robot1")
    active[0] = False
    failed_exit_callback()
    navigation_callbacks[-1]()
    with pytest.raises(ValueError, match="비상정지"):
        ctx.gateway.cmd_vel("robot1", .2, 0)
    ctx.gateway.emergency_release("robot1")
    ctx.gateway.cmd_vel("robot1", .2, 0)
    assert ctx.fleet.get_robot("robot1").state == RobotState.PAUSED
    assert not ctx.sent
