"""앞 노드로의 직진 합류. 계획용 노드 좌표가 아닌 실제 Pose를 검증한다."""

import math

from ..config import (
    STATION_EXIT_MAX_DISTANCE_M, STATION_EXIT_MAX_HEADING_DEG,
    STATION_EXIT_TOLERANCE_M,
)
from .real_navigation import measured_distance


class StationExitError(ValueError):
    reason_code = "station_exit_failed"


def forward_distance(robot, target, now):
    distance = measured_distance(robot, target, now)
    if distance <= STATION_EXIT_TOLERANCE_M:
        return 0.0
    if distance > STATION_EXIT_MAX_DISTANCE_M:
        raise StationExitError("앞 노드까지의 거리가 이탈 허용 거리를 초과했습니다.")
    with robot._lock:
        x, y, yaw = robot.x, robot.y, robot.yaw
    if yaw is None or not math.isfinite(yaw):
        raise StationExitError("로봇 방향을 확인할 수 없습니다.")
    dx, dy = target[0] - x, target[1] - y
    forward = dx * math.cos(yaw) + dy * math.sin(yaw)
    lateral = abs(-dx * math.sin(yaw) + dy * math.cos(yaw))
    angle = abs(math.atan2(lateral, forward))
    if forward <= 0 or lateral > STATION_EXIT_TOLERANCE_M or angle > math.radians(STATION_EXIT_MAX_HEADING_DEG):
        raise StationExitError("앞 노드가 직진 가능한 방향에 없습니다. 초기 위치·방향을 확인하세요.")
    return forward
