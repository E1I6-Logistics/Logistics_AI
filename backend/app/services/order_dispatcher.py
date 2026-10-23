"""주문 대기열에 사용할 로봇 우선순위 선택."""

from ..models.robot import RobotState
from ..ros2.ros_gateway import CHARGING_STATION_NODES, ros_gateway
# 기존 코드. 배터리 전압 하한을 함께 가져오도록 바꾼다.
# from ..config import REAL_POSE_TIMEOUT_S
from ..config import MIN_DISPATCH_BATTERY_VOLTAGE, REAL_POSE_TIMEOUT_S
from time import monotonic

import math


# 배터리 전압으로 배차할 수 없는 이유를 반환한다. 배차할 수 있으면 None이다.
# 전압을 모르면 하한을 지키는지 확인할 수 없으므로 후보에서 뺀다.
def battery_block_reason(voltage):
    if MIN_DISPATCH_BATTERY_VOLTAGE <= 0:
        return None
    try:
        voltage = float(voltage)
    except (TypeError, ValueError):
        return "battery_unknown"
    if not math.isfinite(voltage):
        return "battery_unknown"
    if voltage < MIN_DISPATCH_BATTERY_VOLTAGE:
        return "low_battery"
    return None


# 현재 배차 가능한 로봇 목록을 반환한다.
def available_robots(mode, busy_ids, returning_ids=(), operation_mode="algorithm"):
    from .mode_service import feature_engine

    # 기존에는 algorithm 배차만 허용했다. 후보 필터링은 AI 배차에서도 동일하게 사용한다.
    # if feature_engine("dispatch", operation_mode) != "algorithm":
    #     raise ValueError("지원하지 않는 배차 방식입니다.")

    dispatch_engine = feature_engine("dispatch", operation_mode)
    if dispatch_engine not in {"algorithm", "ai"}:
        raise ValueError(f"지원하지 않는 배차 방식입니다: {dispatch_engine}")

    if mode == "simulation":
        from .mock_data import mock_fms

        rows = mock_fms.robot_snapshots(mode)
        candidates = []
        for row in rows:
            robot_id = row["robot_id"]
            if robot_id in busy_ids or row.get("connection_state") != "ONLINE":
                continue
            if battery_block_reason(row.get("battery_voltage")) is not None:
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
        if battery_block_reason(robot.min_battery_voltage()) is not None:
            continue
        if (
            robot.robot_id in returning_ids
            and robot.occupied_node != CHARGING_STATION_NODES.get(robot.robot_id)
            and robot.state != RobotState.NAVIGATING
        ):
            continue
        if (
            robot.state == RobotState.IDLE
            and robot.goal_node is None
            and robot.route is None
            and not ros_gateway.navigation_active(robot.robot_id)
        ):
            candidates.append((0, robot.robot_id))
        elif (
            robot.robot_id in returning_ids and robot.occupied_node is not None and robot.state == RobotState.NAVIGATING
        ):
            candidates.append((1, robot.robot_id))
    return [robot_id for _, robot_id in sorted(candidates)]
