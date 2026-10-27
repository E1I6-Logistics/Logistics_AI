"""앞 노드로의 직진 합류. 계획용 노드 좌표가 아닌 실제 Pose를 검증한다."""

import math

from ..config import STATION_EXIT_MAX_DISTANCE_M, STATION_EXIT_TOLERANCE_M
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
    # 도킹 자세가 앞 노드를 향한다는 운영 전제에 따라 별도 횡오차·각도 문턱은 두지 않는다.
    # 전진 성분만 요청하고, 실제 앞 노드 도착은 Action 종료 후 원본 Pose로 확인한다.
    if forward <= 0:
        raise StationExitError("앞 노드가 로봇의 전방에 없습니다. 초기 위치·방향을 확인하세요.")
    return forward
