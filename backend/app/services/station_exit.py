"""충전소 직진 이탈 오류를 일반 주행 오류와 구분한다."""


class StationExitError(ValueError):
    reason_code = "station_exit_failed"
