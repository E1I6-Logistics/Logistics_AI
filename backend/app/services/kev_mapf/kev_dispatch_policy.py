"""KEV 로봇 배차 설정. 경로·MAPF의 KEV 호출 설정과 독립적이다."""

import math
import os


def _positive_number(name, default, *, allow_zero=False):
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{name} 설정을 확인하세요.")
    return value


def dispatch_enabled(mode):
    name = "FMS_SIM_KEV_DISPATCH_ENABLED" if mode == "simulation" else "FMS_REAL_KEV_DISPATCH_ENABLED"
    value = os.getenv(name, "1" if mode == "simulation" else "0").strip().lower()
    if value not in {"0", "1", "false", "true"}:
        raise ValueError(f"{name}은 0 또는 1이어야 합니다.")
    return value in {"1", "true"}


def kev_dispatch_distance_tie_meters():
    return _positive_number("KEV_DISPATCH_DISTANCE_TIE_METERS", .12, allow_zero=True)


def dispatch_timeout_seconds():
    return _positive_number("KEV_DISPATCH_TIMEOUT_SECONDS", 3)


def dispatch_cooldown_seconds():
    return _positive_number("KEV_DISPATCH_COOLDOWN_SECONDS", 30, allow_zero=True)
