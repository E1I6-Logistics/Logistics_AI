"""주문 대기열에 사용할 로봇 우선순위 선택."""

from ..models.robot import RobotState
from ..ros2.ros_gateway import CHARGING_STATION_NODES, ros_gateway
from ..config import REAL_POSE_TIMEOUT_S
from .dispatch_settings import dispatch_settings
from time import monotonic


def battery_block_reason(voltage, *, mode="real"):
    return dispatch_settings.battery_block_reason(voltage, mode=mode)


# 현재 배차 가능한 로봇 목록을 반환한다.
def available_robots(mode, busy_ids, returning_ids=(), operation_mode="algorithm"):
    from .mode_service import feature_engine

    if feature_engine("dispatch", operation_mode, mode=mode) not in {"algorithm", "ai"}:
        raise ValueError("지원하지 않는 배차 방식입니다.")
    if mode == "simulation":
        from .mock_data import mock_fms

        rows = mock_fms.robot_snapshots(mode)
        candidates = []
        for row in rows:
            robot_id = row["robot_id"]
            if robot_id in busy_ids or row.get("connection_state") != "ONLINE":
                continue
            if battery_block_reason(row.get("battery_voltage"), mode="simulation") is not None:
                continue
            if (
                robot_id in returning_ids
                and row.get("occupied_node") != CHARGING_STATION_NODES.get(robot_id)
                and row.get("goal_node") is None
            ):
                continue
            if (
                row.get("status") == "IDLE"
                and row.get("goal_node") is None
                and row.get("route") is None
                and row.get("occupied_node") is not None
                and row.get("occupied_edge") is None
            ):
                candidates.append((0, robot_id))
            elif (
                robot_id in returning_ids
                and row.get("goal_node") is not None
                and row.get("occupied_node") is not None
                and row.get("occupied_edge") is None
            ):
                candidates.append((1, robot_id))
        return [robot_id for _, robot_id in sorted(candidates)]

    from .fleet_manager import fleet_manager

    candidates = []
    for robot in fleet_manager.get_all_robots():
        if (
            robot.robot_id in busy_ids
            or not robot.connected
            or robot.order_id is not None
            or robot.occupied_node is None
            or robot.pose_received_at is None
            or monotonic() - robot.pose_received_at > REAL_POSE_TIMEOUT_S
        ):
            continue
        if not ros_gateway.dock_dispatch_allowed(robot.robot_id):
            continue
        if battery_block_reason(robot.min_battery_voltage(), mode="real") is not None:
            continue
        if (
            robot.robot_id in returning_ids
            and robot.occupied_node != CHARGING_STATION_NODES.get(robot.robot_id)
            and robot.state != RobotState.NAVIGATING
        ):
            continue
        if (
            (robot.state == RobotState.IDLE or ros_gateway.charging_dispatch_ready(robot.robot_id))
            and robot.goal_node is None
            and robot.route is None
            and not ros_gateway.navigation_active(robot.robot_id)
        ):
            candidates.append((1 if robot.state == RobotState.CHARGING else 0, robot.robot_id))
        elif (
            robot.robot_id in returning_ids and robot.occupied_node is not None and robot.state == RobotState.NAVIGATING
        ):
            candidates.append((2, robot.robot_id))
    return [robot_id for _, robot_id in sorted(candidates)]
