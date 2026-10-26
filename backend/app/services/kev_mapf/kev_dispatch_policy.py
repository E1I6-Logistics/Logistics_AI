"""KEV 로봇 배차와 주문 우선순위에 공통으로 쓰는 정책 설정."""

import math
import os


KEV_DISPATCH_TIMEOUT_SECONDS = float(os.getenv("KEV_DISPATCH_TIMEOUT_SECONDS", "3"))
KEV_DISPATCH_COOLDOWN_SECONDS = float(os.getenv("KEV_DISPATCH_COOLDOWN_SECONDS", "30"))
KEV_DISPATCH_MAX_ROUNDS = 4

# near_distance는 운영 기본값, 나머지는 기존 ETA 비교 실험용 범위다.
KEV_DISPATCH_CANDIDATE_SCOPES = {"near_distance", "near_eta", "all_eligible"}


class KevDispatchEtaRejected(RuntimeError):
    """all_eligible KEV 선택이 ETA 상한을 넘어 코드가 거부한 경우."""


def kev_dispatch_eta_tie_seconds():
    """ETA 기반 비교 실험에서 사용할 근소 ETA 범위(초)를 읽는다."""
    seconds = float(os.getenv("KEV_DISPATCH_ETA_TIE_SECONDS", "2"))
    if seconds < 0:
        raise ValueError("KEV_DISPATCH_ETA_TIE_SECONDS는 0 이상이어야 합니다.")
    return seconds


def kev_dispatch_distance_tie_meters():
    """최단 route graph 거리보다 이 거리(m) 이내인 로봇만 KEV 후보로 둔다."""
    meters = float(os.getenv("KEV_DISPATCH_DISTANCE_TIE_METERS", "0.12"))
    if not math.isfinite(meters) or meters < 0:
        raise ValueError("KEV_DISPATCH_DISTANCE_TIE_METERS는 0 이상이어야 합니다.")
    return meters


def kev_dispatch_eta_speed_mps():
    """ETA 기반 비교 실험과 주문 우선순위 KEV의 속도(m/s)를 읽는다."""
    speed_mps = float(os.getenv("KEV_DISPATCH_ETA_SPEED_MPS", "0.2"))
    if speed_mps <= 0:
        raise ValueError("KEV_DISPATCH_ETA_SPEED_MPS는 0보다 커야 합니다.")
    return speed_mps


def kev_dispatch_candidate_scope():
    """로봇 배차 KEV 후보 범위를 읽는다. 기본은 거리 기준이다."""
    scope = os.getenv("KEV_DISPATCH_CANDIDATE_SCOPE", "near_distance").strip().lower()
    if scope not in KEV_DISPATCH_CANDIDATE_SCOPES:
        raise ValueError("KEV_DISPATCH_CANDIDATE_SCOPE는 near_distance, near_eta 또는 all_eligible이어야 합니다.")
    return scope


def kev_dispatch_recent_workload_enabled():
    """최근 작업량을 KEV 로봇 배차 입력에 포함할지 읽는다."""
    value = os.getenv("KEV_DISPATCH_RECENT_WORKLOAD", "0").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError("KEV_DISPATCH_RECENT_WORKLOAD는 true 또는 false여야 합니다.")


def kev_dispatch_workload_window_seconds():
    """최근 작업량을 집계할 기간(초)을 읽는다."""
    seconds = float(os.getenv("KEV_DISPATCH_WORKLOAD_WINDOW_SECONDS", "600"))
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("KEV_DISPATCH_WORKLOAD_WINDOW_SECONDS는 0보다 커야 합니다.")
    return seconds


def kev_order_scheduling_enabled():
    """대기 주문 우선순위 KEV 실험의 사용 여부를 읽는다."""
    value = os.getenv("KEV_ORDER_SCHEDULING_ENABLED", "0").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError("KEV_ORDER_SCHEDULING_ENABLED는 true 또는 false여야 합니다.")


def kev_order_scheduling_max_wait_seconds():
    """FIFO를 우선할 최대 대기 시간(초)을 읽는다."""
    seconds = float(os.getenv("KEV_ORDER_SCHEDULING_MAX_WAIT_SECONDS", "120"))
    if seconds < 0:
        raise ValueError("KEV_ORDER_SCHEDULING_MAX_WAIT_SECONDS는 0 이상이어야 합니다.")
    return seconds


def kev_order_scheduling_max_wait_gap_seconds():
    """KEV 선택보다 더 오래 기다린 주문을 보호할 차이(초)를 읽는다."""
    seconds = float(os.getenv("KEV_ORDER_SCHEDULING_MAX_WAIT_GAP_SECONDS", "30"))
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("KEV_ORDER_SCHEDULING_MAX_WAIT_GAP_SECONDS는 0 이상이어야 합니다.")
    return seconds


def order_scheduling_guard(pending_orders, chosen_label, max_wait_gap_seconds, eta_gap_seconds):
    """KEV가 고른 다음 주문이 명백히 불리하면 코드가 대체 주문을 반환한다."""
    by_label = {profile["order_label"]: profile for profile in pending_orders}
    chosen = by_label[chosen_label]
    longer_waiting = [
        profile
        for profile in pending_orders
        if profile["waiting_seconds"] >= chosen["waiting_seconds"] + max_wait_gap_seconds
    ]
    if longer_waiting:
        oldest = min(longer_waiting, key=lambda profile: profile["queue_position"])
        return oldest["order_label"], "wait"

    chosen_eta = chosen["minimum_pickup_eta_seconds"]
    if chosen_eta is None:
        return None, None
    faster = [
        profile
        for profile in pending_orders
        if profile["minimum_pickup_eta_seconds"] is not None
        and profile["minimum_pickup_eta_seconds"] < chosen_eta - eta_gap_seconds
        and profile["waiting_seconds"] > chosen["waiting_seconds"] - max_wait_gap_seconds
    ]
    if faster:
        best = min(faster, key=lambda profile: (profile["minimum_pickup_eta_seconds"], profile["queue_position"]))
        return best["order_label"], "eta"
    return None, None
