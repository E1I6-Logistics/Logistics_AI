"""주문 대기열에 사용할 로봇 우선순위 선택."""

from ..models.robot import RobotState
from ..ros2.ros_gateway import CHARGING_STATION_NODES, ros_gateway
from ..config import REAL_POSE_TIMEOUT_S
from time import monotonic


def available_robots(mode, busy_ids, returning_ids=(), operation_mode="algorithm"):
    from .mode_service import feature_engine
    # AI dispatch can be connected here later; both modes currently use this policy.
    if feature_engine("dispatch", operation_mode) != "algorithm":
        raise ValueError("지원하지 않는 배차 방식입니다.")
    if mode == "simulation":
        from .mock_data import mock_fms
        rows = mock_fms.robot_snapshots(mode)
        candidates = []
        for row in rows:
            robot_id = row["robot_id"]
            if robot_id in busy_ids or row.get("connection_state") != "ONLINE":
                continue
            if (robot_id in returning_ids and row.get("occupied_node") != CHARGING_STATION_NODES.get(robot_id)
                    and row.get("goal_node") is None):
                continue
            if (row.get("status") == "IDLE" and row.get("goal_node") is None
                    and row.get("route") is None and row.get("occupied_node") is not None
                    and row.get("occupied_edge") is None):
                candidates.append((0, robot_id))
            elif (robot_id in returning_ids and row.get("goal_node") is not None
                  and row.get("occupied_node") is not None
                  and row.get("occupied_edge") is None):
                candidates.append((1, robot_id))
        return [robot_id for _, robot_id in sorted(candidates)]

    from .fleet_manager import fleet_manager
    candidates = []
    for robot in fleet_manager.get_all_robots():
        if (robot.robot_id in busy_ids or not robot.connected or robot.order_id is not None
                or robot.occupied_node is None or robot.pose_received_at is None
                or monotonic() - robot.pose_received_at > REAL_POSE_TIMEOUT_S):
            continue
        if (robot.robot_id in returning_ids
                and robot.occupied_node != CHARGING_STATION_NODES.get(robot.robot_id)
                and robot.state != RobotState.NAVIGATING):
            continue
        if (robot.state == RobotState.IDLE and robot.goal_node is None
                and robot.route is None and not ros_gateway.navigation_active(robot.robot_id)):
            candidates.append((0, robot.robot_id))
        elif (robot.robot_id in returning_ids and robot.occupied_node is not None
              and robot.state == RobotState.NAVIGATING):
            candidates.append((1, robot.robot_id))
    return [robot_id for _, robot_id in sorted(candidates)]
