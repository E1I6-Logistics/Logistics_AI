"""완료한 실제 주행이 배차 가능 상태를 남기는지 검증한다."""

from action_msgs.msg import GoalStatus

import backend.app.ros2.ros_gateway as ros_gateway_module
from backend.app.models.robot import NavigationType, Robot, RobotState


def _arrived_robot(navigation_type: NavigationType) -> Robot:
    robot = Robot("robot-test")
    robot.connected = True
    robot.state = RobotState.NAVIGATING
    robot.goal_node = "5"
    robot.navigation_type = navigation_type
    robot.route = {"node_ids": ["2", "5"]}
    robot.x = 0.0
    robot.y = 0.0
    return robot


def _complete_navigation(monkeypatch, robot: Robot) -> None:
    gateway = ros_gateway_module.RosGateway()
    monkeypatch.setattr(ros_gateway_module.fleet_manager, "get_robot", lambda _robot_id: robot)
    monkeypatch.setattr(ros_gateway_module, "get_node", lambda _node_id: {"x": 0.0, "y": 0.0})
    monkeypatch.setattr(ros_gateway_module, "measured_distance", lambda *_args: 0.0)
    monkeypatch.setattr(ros_gateway_module, "ARUCO_MARKER_IDS", {})

    gateway.on_navigation_result(robot.robot_id, GoalStatus.STATUS_SUCCEEDED)


def test_completed_goal_node_navigation_clears_goal_for_future_dispatch(monkeypatch):
    robot = _arrived_robot(NavigationType.GOAL)

    _complete_navigation(monkeypatch, robot)

    assert robot.current_node == "5"
    assert robot.goal_node is None
    assert robot.route is None
    assert robot.navigation_type is None
    assert robot.state == RobotState.IDLE


def test_completed_return_navigation_clears_goal_for_future_dispatch(monkeypatch):
    robot = _arrived_robot(NavigationType.RETURN)

    _complete_navigation(monkeypatch, robot)

    assert robot.current_node == "5"
    assert robot.goal_node is None
    assert robot.route is None
    assert robot.navigation_type is None
    assert robot.state == RobotState.IDLE


def test_completed_precision_dock_clears_goal_for_future_dispatch(monkeypatch):
    robot = _arrived_robot(NavigationType.CHARGING)
    robot.state = RobotState.DOCKING
    gateway = ros_gateway_module.RosGateway()
    monkeypatch.setattr(ros_gateway_module.fleet_manager, "get_robot", lambda _robot_id: robot)

    gateway.on_precision_dock_result(robot.robot_id, GoalStatus.STATUS_SUCCEEDED)

    assert robot.current_node == "5"
    assert robot.goal_node is None
    assert robot.route is None
    assert robot.navigation_type is None
    assert robot.state == RobotState.IDLE
