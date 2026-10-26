"""heeyeon의 near_distance 배차를 dev 주문 실행과 연결한다.

후보 수집/재검증은 OrderManager의 navigation -> order 잠금 안에서 수행한다.
이 모듈의 KEV 작업은 사본만 읽고 로봇·주문·예약 상태를 변경하지 않는다.
"""

import asyncio
import logging
import math
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from time import monotonic

from .fleet_manager import fleet_manager
from .kev_mapf.kev_dispatch_client import request_choice
from .kev_mapf.kev_dispatch_policy import (
    dispatch_cooldown_seconds,
    dispatch_timeout_seconds,
    kev_dispatch_distance_tie_meters,
)
from .route_graph import load_route_graph
from .route_service import plan_route
from .mode_service import mode_manager

logger = logging.getLogger(__name__)


def robot_profile(robot_id, mode, returning):
    if mode == "simulation":
        from .mock_data import mock_fms

        row = mock_fms.robot_snapshot(robot_id, mode)
        state = row["status"]
        epoch = row.get("command_epoch")
    else:
        robot = fleet_manager.get_robot(robot_id)
        row = {key: getattr(robot, key) for key in (
            "battery", "current_node", "occupied_node", "occupied_edge", "goal_node", "navigation_id"
        )}
        state = robot.state.value
        epoch = robot.navigation_id
    return {
        "robot_id": robot_id,
        "state": state,
        "battery": row.get("battery"),
        "current_node": row.get("current_node"),
        "occupied_node": row.get("occupied_node"),
        "goal_node": row.get("goal_node"),
        "returning": robot_id in returning,
        "_epoch": epoch,
    }


def rule_choice(profiles):
    """heeyeon fallback: IDLE·비복귀, 현재 잔량 내림차순, ID 오름차순."""
    def priority(profile):
        battery = profile.get("battery")
        try:
            battery = float(battery)
        except (TypeError, ValueError):
            battery = math.nan
        valid = math.isfinite(battery)
        return (
            not (profile["state"] == "IDLE" and not profile["returning"]),
            not valid,
            -battery if valid else 0,
            profile["robot_id"],
        )

    return min(profiles, key=priority)["robot_id"] if profiles else None


def prepare_dispatch(row, candidates, mode, returning):
    """첫 실제 방문 픽업까지의 graph 거리로 후보를 제한한다. 위치·상태별 hard tier는 없다."""
    candidates = list(candidates)
    if len(candidates) > 1 and row.get("last_robot_id") in candidates:
        candidates.remove(row["last_robot_id"])
    profiles = []
    for robot_id in sorted(candidates):
        profile = robot_profile(robot_id, mode, returning)
        try:
            distance = float(plan_route(str(profile["current_node"]), str(row["pickup_nodes"][0]))["cost"])
            if not math.isfinite(distance) or distance < 0:
                raise ValueError("유효하지 않은 거리")
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            # 계산 실패를 이유로 거리 제한을 우회하지 않는다. 다른 유효 후보는 계속 비교한다.
            logger.warning("dispatch distance unavailable: order=%s robot=%s error=%s", row["order_id"], robot_id, exc)
            continue
        profile.update(pickup_node=str(row["pickup_nodes"][0]), pickup_distance_m=distance)
        profiles.append(profile)
    if not profiles:
        return None
    minimum = min(profile["pickup_distance_m"] for profile in profiles)
    window = kev_dispatch_distance_tie_meters()
    profiles = [profile for profile in profiles if profile["pickup_distance_m"] <= minimum + window + 1e-9]
    for profile in profiles:
        profile["distance_delta_m"] = profile["pickup_distance_m"] - minimum
    public_profiles = [{key: value for key, value in p.items() if not key.startswith("_")} for p in profiles]
    choices = {
        p["robot_id"]: (
            f"state={p['state']}, returning={p['returning']}, battery={p['battery']}, "
            f"current_node={p['current_node']}, occupied_node={p['occupied_node']}, goal_node={p['goal_node']}, "
            f"pickup_node={p['pickup_node']}, pickup_distance_m={p['pickup_distance_m']:.3f}, "
            f"distance_delta_m={p['distance_delta_m']:.3f}"
        ) for p in profiles
    }
    # 최신 후보 필터로 안전 조건을 재검증한다. 상태·노드·주행 세대 변경은 이전 응답을 무효화한다.
    # Battery is contextual; eligibility is recomputed every tick, including voltage freshness.
    fingerprint = tuple((p["robot_id"], p["state"], p["current_node"], p["occupied_node"],
                         p["goal_node"], p["returning"], p["_epoch"]) for p in profiles)
    return {
        "order_id": row["order_id"],
        "mode": mode,
        "key": (mode, row["order_id"], row.get("retry_count", 0), tuple(row["pickup_nodes"]),
                row["operation_mode"], mode_manager.generation, fingerprint, deepcopy(load_route_graph())),
        "candidates": list(choices),
        "baseline": rule_choice(profiles),
        "debug_record": {
            "order_id": row["order_id"],
            "question_id": f"dispatch_order_{row.get('_queue_sequence', 0)}_retry_{row.get('retry_count', 0)}",
            "state": {
                "order": {key: row.get(key) for key in (
                    "items", "total_quantity", "pickup_nodes", "workstation_node", "last_robot_id"
                )},
                "robots": public_profiles,
                "dispatch_context": {
                    "distance_filter_applied": True,
                    "minimum_pickup_distance_m": minimum,
                    "distance_tie_window_meters": window,
                    "post_task_battery_available": False,
                    "recent_workload_available": False,
                },
            },
            "choices": choices,
            "instructions": (
                "All listed robots have passed code safety checks and are within the configured route graph distance near-tie window. "
                "Choose exactly one listed candidate ID. Do not re-rank candidates by pickup distance and do not treat battery as a safety threshold. "
                "Prefer an IDLE, non-returning robot when operational state differs. Use the current battery only as secondary context; "
                "it is not an estimated post-task battery. CHARGING candidates have passed dock departure checks. "
                "Return only one candidate ID."
            ),
        },
    }


class KevRobotDispatcher:
    """한 번에 한 FIFO 주문만 요청한다. tick은 HTTP를 기다리지 않는다."""

    def __init__(self):
        self._task = None
        self._request = None
        self._retry_at = {}
        self.selector = None
        self._executor = None

    def discard(self):
        if self._task is not None:
            loop = self._task.get_loop()
            if not loop.is_closed():
                try:
                    current_loop = asyncio.get_running_loop()
                except RuntimeError:
                    current_loop = None
                if current_loop is loop:
                    self._task.cancel()
                else:
                    # ROS/MQTT callbacks may run outside the asyncio thread.
                    loop.call_soon_threadsafe(self._task.cancel)
        self._task = self._request = None

    def discard_order(self, order_id):
        if self._request is not None and self._request["order_id"] == order_id:
            self.discard()

    def close(self):
        self.discard()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        self.selector = None

    async def _run(self, request, timeout):
        # The outer deadline covers metadata + inference together, not each HTTP separately.
        try:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kev-robot-dispatch")
            worker = asyncio.get_running_loop().run_in_executor(
                self._executor,
                partial(request_choice, request, self.selector, timeout_seconds=timeout,
                        logger=logger, debug_prefix="KEV_DISPATCH_DEBUG"),
            )
            result, selector = await asyncio.wait_for(
                worker, timeout=timeout
            )
            choice = result.get("choice")
            if choice not in request["candidates"]:
                raise ValueError(f"KEV 배차 후보가 아닌 응답: {choice}")
            return {"choice": choice, "selector": selector}
        except Exception as exc:
            return {"error": exc}

    def choose(self, request):
        """최신 검사를 통과한 request만 받는다. None은 해당 FIFO 주문의 응답 대기이다."""
        if request is None:
            self.discard()
            return None
        # Record failures before snapshot invalidation so changed candidates cannot bypass cooldown.
        result = None
        if self._task is not None and self._task.done():
            if not self._task.cancelled():
                result = self._task.result()
                # 완료된 정상 요청의 client만 재사용한다. timeout worker의 늦은 결과는 반영하지 않는다.
                if "selector" in result:
                    self.selector = result["selector"]
                if "error" in result:
                    mode = self._request["mode"]
                    self._retry_at[mode] = monotonic() + dispatch_cooldown_seconds()
                    logger.warning("KEV dispatch failed; rule fallback: order=%s cooldown=%ss error=%r",
                                   self._request["order_id"], dispatch_cooldown_seconds(), result["error"])
        if self._request is not None and self._request["key"] != request["key"]:
            logger.info("KEV dispatch result discarded: order or robot state changed")
            self.discard()
            result = None
        if len(request["candidates"]) == 1 or monotonic() < self._retry_at.get(request["mode"], 0):
            self.discard()
            return request["baseline"]
        if result is not None:
            self.discard()
            return result.get("choice", request["baseline"])
        if self._task is None:
            self._request = request
            self._task = asyncio.create_task(self._run(request, dispatch_timeout_seconds()))
        return None
