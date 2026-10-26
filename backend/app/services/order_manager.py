"""공통 주문 대기열, 배차, Real 작업 단계."""

import asyncio
import json
import logging
import math
import os
from collections import deque
from datetime import datetime, timezone
from threading import RLock
from time import monotonic
from uuid import uuid4

from ..config import REAL_POSE_TIMEOUT_S
from ..models.robot import RobotState
from ..ros2.ros_gateway import CHARGING_STATION_NODES, NODE_OMX_MAP, ros_gateway
from ..schemas.robot import normalize_robot_id
from .fleet_manager import fleet_manager
from .mode_service import mode_manager, feature_engine
from .mqtt_manager import mqtt_manager
# 기존 코드. 배터리 전압 차단 이유도 같은 함수로 계산하도록 바꾼다.
# from .order_dispatcher import available_robots
from .order_dispatcher import available_robots, battery_block_reason
from .order_route_dispatcher import dispatch_order_route
from .route_service import plan_route
from .scenario_service import scenario_manager
from .workstation_manager import workstation_manager


logger = logging.getLogger(__name__)

# 기존 주석. KEV 호출을 lock 밖 스레드로 옮겨서 tick이 이벤트 루프를 막지 않는다.
# # 배차 KEV 호출은 주문 tick 안에서 동기로 실행되므로 짧게 끊고, 실패 후에는 잠시 호출하지 않는다.
# 배차 KEV 호출은 tick 한 번을 그만큼 늦추므로 짧게 끊고, 실패 후에는 잠시 호출하지 않는다.
KEV_DISPATCH_TIMEOUT_SECONDS = float(os.getenv("KEV_DISPATCH_TIMEOUT_SECONDS", "3"))
KEV_DISPATCH_COOLDOWN_SECONDS = float(os.getenv("KEV_DISPATCH_COOLDOWN_SECONDS", "30"))
# tick 한 번에서 KEV 결과를 받아 배차를 다시 시도하는 최대 횟수. 남은 주문은 다음 tick에서 처리한다.
KEV_DISPATCH_MAX_ROUNDS = 4
# /api/robots 조회가 배차 tick을 기다리지 않도록 주문 lock 대기 시간을 짧게 둔다.
DISPATCH_STATUS_LOCK_TIMEOUT_S = 0.05


def _kev_dispatch_eta_tie_seconds():
    """Return the configured ETA window, rejecting an invalid policy value."""
    seconds = float(os.getenv("KEV_DISPATCH_ETA_TIE_SECONDS", "2"))
    if seconds < 0:
        raise ValueError("KEV_DISPATCH_ETA_TIE_SECONDS는 0 이상이어야 합니다.")
    return seconds


def _kev_dispatch_eta_speed_mps():
    """Return the ETA conversion speed configured for the active deployment."""
    speed_mps = float(os.getenv("KEV_DISPATCH_ETA_SPEED_MPS", "0.2"))
    if speed_mps <= 0:
        raise ValueError("KEV_DISPATCH_ETA_SPEED_MPS는 0보다 커야 합니다.")
    return speed_mps


class KevDispatchEtaRejected(RuntimeError):
    """KEV가 유효 후보를 골랐지만 ETA 상한을 넘어 코드가 거부한 경우. 서버 오류가 아니므로 cooldown을 걸지 않는다."""


# KEV가 비교할 후보 범위. near_eta는 최단 ETA + 근소 범위 안의 후보만, all_eligible은 안전 검사를 통과한 후보 전체.
KEV_DISPATCH_CANDIDATE_SCOPES = {"near_eta", "all_eligible"}


def _kev_dispatch_candidate_scope():
    """Return the configured KEV candidate scope for dispatch experiments."""
    scope = os.getenv("KEV_DISPATCH_CANDIDATE_SCOPE", "near_eta").strip().lower()
    if scope not in KEV_DISPATCH_CANDIDATE_SCOPES:
        raise ValueError("KEV_DISPATCH_CANDIDATE_SCOPE는 near_eta 또는 all_eligible이어야 합니다.")
    return scope


def _kev_dispatch_recent_workload_enabled():
    """로봇별 최근 작업량을 KEV 배차 입력에 넣을지 읽는다. 기본은 꺼져 기존 KEV 입력과 같다."""
    value = os.getenv("KEV_DISPATCH_RECENT_WORKLOAD", "0").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError("KEV_DISPATCH_RECENT_WORKLOAD는 true 또는 false여야 합니다.")


def _kev_dispatch_workload_window_seconds():
    """최근 작업량을 셀 기간(초)을 읽는다."""
    seconds = float(os.getenv("KEV_DISPATCH_WORKLOAD_WINDOW_SECONDS", "600"))
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("KEV_DISPATCH_WORKLOAD_WINDOW_SECONDS는 0보다 커야 합니다.")
    return seconds


def _kev_order_scheduling_enabled():
    """대기 주문 우선순위 KEV 실험의 사용 여부를 읽는다."""
    value = os.getenv("KEV_ORDER_SCHEDULING_ENABLED", "0").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError("KEV_ORDER_SCHEDULING_ENABLED는 true 또는 false여야 합니다.")


def _kev_order_scheduling_max_wait_seconds():
    """주문 starvation을 막기 위해 KEV보다 FIFO를 우선할 최대 대기 시간을 읽는다."""
    seconds = float(os.getenv("KEV_ORDER_SCHEDULING_MAX_WAIT_SECONDS", "120"))
    if seconds < 0:
        raise ValueError("KEV_ORDER_SCHEDULING_MAX_WAIT_SECONDS는 0 이상이어야 합니다.")
    return seconds


class OrderManager:
    # OrderManager의 초기 상태를 설정한다.
    def __init__(self):
        self.lock = RLock()
        self.orders = {}
        self.returning = {}  # robot_id -> completed order_id
        self.recovering = {}  # robot_id -> failed order_id
        self._return_started = set()
        self._route_tasks = {}
        self._kev_dispatch_selector = None
        self._kev_dispatch_retry_at = 0.0
        self._kev_order_scheduling_retry_at = 0.0
        # 배정 기록 (monotonic 시각, robot_id, 배정 시 첫 픽업까지 route graph 거리 또는 None). 최근 작업량 집계에 쓴다.
        self._dispatch_history = deque()
        self._order_queue_sequence = 0

    # 주문 품목에 필요한 픽업대 노드를 반환한다.
    @staticmethod
    def get_pickup_nodes(items):
        nodes = []
        if items.get("A", 0) or items.get("B", 0):
            nodes.append("5")
        if items.get("C", 0) or items.get("D", 0):
            nodes.append("6")
        return nodes

    # 주문 내용을 검증하고 대기열에 등록한다.
    def create_order(
        self,
        items,
        total_quantity,
        order_name=None,
        driving_mode=None,
        selector=None,
        model=None,
        operation_mode=None,
    ):
        scenario_manager.ensure_available()

        # 기존 코드. driving_mode가 넘어오면 operation_mode를 역추론해서 AI 모드 주문이 algorithm으로 저장됐다.
        # operation_mode = mode_manager.operation_mode
        # if driving_mode is None:
        #     driving_mode = "llm" if feature_engine("route", operation_mode) == "ai" else "standard"
        # else:
        #     # Preserve callers that already supply the legacy route mode.
        #     operation_mode = "ai" if driving_mode == "llm" else "algorithm"

        if operation_mode is None:
            operation_mode = mode_manager.operation_mode
            if driving_mode is not None:
                # Preserve callers that already supply the legacy route mode.
                # AI 모드도 경로는 algorithm일 수 있으므로 operation_mode를 넘기는 호출은 역추론하지 않는다.
                operation_mode = "ai" if driving_mode == "llm" else "algorithm"

        if driving_mode is None:
            driving_mode = "llm" if feature_engine("route", operation_mode) == "ai" else "standard"
        if set(items) != {"A", "B", "C", "D"} or any(
            not isinstance(value, int) or value < 0 for value in items.values()
        ):
            raise ValueError("A/B/C/D 수량을 0 이상의 정수로 입력하세요.")
        if sum(items.values()) != total_quantity or not 1 <= total_quantity <= 5:
            raise ValueError("총 수량은 품목 합계와 같고 1~5개여야 합니다.")
        if driving_mode not in ("standard", "llm"):
            raise ValueError("지원하지 않는 Planning Mode입니다.")
        if driving_mode == "llm":
            from .route_model_catalog import require_enabled_model

            if not selector or not model:
                raise ValueError("AI 경로 모델을 선택하세요.")
            require_enabled_model(selector, model)
        with scenario_manager.lock, self.lock:
            scenario_manager.ensure_available()
            order_id = str(uuid4())
            self._order_queue_sequence += 1
            row = {
                "order_id": order_id,
                "order_name": (order_name or "").strip() or f"ORDER-{len(self.orders) + 1:03d}",
                "items": dict(items),
                "total_quantity": total_quantity,
                "pickup_nodes": self.get_pickup_nodes(items),
                "pickup_index": 0,
                "workstation_node": None,
                "robot_id": None,
                "status": "PENDING",
                "phase": "PENDING",
                "mode": mode_manager.mode,
                "driving_mode": driving_mode,
                "operation_mode": operation_mode,
                "selector": selector,
                "model": model,
                "route_started": False,
                # API에는 노출하지 않는 큐 순서와 대기 시작 시각이다. 주문 우선순위 KEV 입력과 FIFO fallback에 쓴다.
                "_queue_sequence": self._order_queue_sequence,
                "_queued_at_monotonic": monotonic(),
            }
            self.orders[order_id] = row
            return self.public_order(row)

    # 내부 상태를 제외한 주문 정보를 반환한다.
    @staticmethod
    def public_order(row):
        return {key: value for key, value in row.items() if key != "route_started" and not key.startswith("_")}

    # 실패한 주문을 다시 대기 상태로 돌린다.
    def retry_order(self, order_id):
        with scenario_manager.lock, self.lock:
            scenario_manager.ensure_available()
            row = self.orders.get(order_id)
            if row is None or row["status"] != "FAILED":
                raise ValueError("실패한 주문만 재시도할 수 있습니다.")
            if row["mode"] != "simulation" or mode_manager.mode != "simulation":
                raise ValueError("실제 주문은 적재물과 OMX 작업 결과 확인 후 재등록하세요.")
            if row["operation_mode"] != mode_manager.operation_mode:
                raise ValueError("주문을 등록했던 AI/알고리즘 운영 모드로 전환한 뒤 재시도하세요.")
            if order_id in self.recovering.values():
                raise ValueError("실패 로봇의 복귀가 끝난 뒤 재시도하세요.")
            if row.get("recovery_status") in ("BLOCKED", "STOPPED"):
                from .mock_data import mock_fms

                snapshot = mock_fms.robot_snapshot(row["robot_id"], "simulation")
                if (
                    snapshot["occupied_node"] != CHARGING_STATION_NODES.get(row["robot_id"])
                    or snapshot["goal_node"] is not None
                    or snapshot["route"] is not None
                ):
                    raise ValueError("실패 로봇을 담당 스테이션으로 복귀시킨 뒤 재시도하세요.")
            row.update(
                last_robot_id=row["robot_id"],
                robot_id=None,
                status="PENDING",
                phase="PENDING",
                pickup_index=0,
                workstation_node=None,
                route_started=False,
                _held_node=None,
            )
            # 재시도해도 기존 dict 삽입 순서가 유지되던 동작과 같게 큐 순서·대기 시작 시각은 바꾸지 않는다.
            row["retry_count"] = row.get("retry_count", 0) + 1
            row.pop("error", None)
            row.pop("recovery_status", None)
            row.pop("recovery_error", None)
            return self.public_order(row)

    # 등록된 주문 목록을 반환한다.
    def list_orders(self):
        with self.lock:
            return [self.public_order(row) for row in self.orders.values()]

    # 주문 ID로 주문 정보를 조회한다.
    def get_order(self, order_id):
        with self.lock:
            row = self.orders.get(order_id)
            return self.public_order(row) if row else None

    # 주문과 로봇이 현재 작업 단계에 해당하는지 확인한다.
    def is_current(self, order_id, robot_id, phase=None):
        with self.lock:
            row = self.orders.get(order_id)
            return bool(
                row
                and row["robot_id"] == robot_id
                and row["status"] == "IN_PROGRESS"
                and (phase is None or row["phase"] == phase)
                and mode_manager.mode == row["mode"]
            )

    # 주문의 진행 단계를 변경한다.
    def set_phase(self, order_id, phase, expected=None):
        with self.lock:
            row = self.orders.get(order_id)
            if row and row["status"] == "IN_PROGRESS" and (expected is None or row["phase"] == expected):
                row["phase"] = phase

    # 주문을 취소하고 배정 로봇을 스테이션으로 복귀시킨다.
    def cancel_order(self, order_id, return_to_station=True):
        with self.lock:
            row = self.orders.get(order_id)
            if row is None or row["status"] in ("COMPLETED", "CANCELLED"):
                return False
            robot_id = row["robot_id"]
            recovering = self.recovering.get(robot_id) == order_id
            owns_robot = row["status"] == "IN_PROGRESS" or recovering
            if row["status"] == "FAILED" and return_to_station and robot_id and not recovering:
                busy_elsewhere = any(other["order_id"] != order_id and other["robot_id"] == robot_id and other["status"] == "IN_PROGRESS" for other in self.orders.values())
                if not busy_elsewhere and row["mode"] == mode_manager.mode:
                    if row["mode"] == "simulation":
                        from .mock_data import mock_fms

                        snapshot = mock_fms.robot_snapshot(robot_id, "simulation")
                        owns_robot = snapshot["occupied_node"] != CHARGING_STATION_NODES.get(robot_id)
                    else:
                        robot = fleet_manager.get_robot(robot_id)
                        owns_robot = robot is not None and robot.occupied_node != CHARGING_STATION_NODES.get(robot_id)
            row["status"] = row["phase"] = "CANCELLED"
            workstation_manager.release(order_id)
            if not owns_robot:
                return True
            mode = row["mode"]
            if return_to_station:
                self.recovering[robot_id] = order_id
                if not recovering:
                    row["recovery_status"] = "PENDING"
                row.pop("recovery_error", None)
            else:
                self.recovering.pop(robot_id, None)
                row["recovery_status"] = "STOPPED"
        if robot_id and not recovering:
            if mode == "simulation":
                from .mock_data import mock_fms

                mock_fms.stop_robot(robot_id)
            else:
                robot = fleet_manager.get_robot(robot_id)
                if robot and robot.state != RobotState.EMERGENCY_STOP and robot.connected:
                    try:
                        callback = lambda: None
                        if return_to_station:
                            callback = lambda oid=order_id, rid=robot_id: self._cancel_return_ready(oid, rid)
                        ros_gateway.cancel_current_work(robot_id, callback)
                    except (ValueError, RuntimeError) as exc:
                        if return_to_station:
                            row["recovery_status"] = "BLOCKED"
                            row["recovery_error"] = str(exc)
                elif return_to_station:
                    row["recovery_status"] = "BLOCKED"
                    row["recovery_error"] = "로봇 연결 또는 긴급 정지 상태를 확인하세요."
                if robot and robot.order_id == order_id:
                    robot.clear_order()
        return True

    # 실제 로봇의 이전 작업 종료가 확인되면 복귀를 시작할 수 있게 한다.
    def _cancel_return_ready(self, order_id, robot_id):
        with self.lock:
            if self.recovering.get(robot_id) != order_id or self.orders[order_id]["status"] != "CANCELLED":
                return
            self.orders[order_id]["recovery_status"] = "RETURNING"
            self.returning[robot_id] = order_id
            robot = fleet_manager.get_robot(robot_id)
            if robot:
                robot.returning = True

    # 로봇에 배정된 주문을 취소한다.
    def cancel_robot(self, robot_id, mode):
        robot_id = normalize_robot_id(robot_id)
        with self.lock:
            failed_id = self.recovering.pop(robot_id, None)
            if failed_id in self.orders:
                self.orders[failed_id]["recovery_status"] = "STOPPED"
            completed_id = self.returning.pop(robot_id, None)
            self._return_started.discard(robot_id)
            if completed_id in self.orders and self.orders[completed_id]["status"] == "COMPLETED":
                self.orders[completed_id]["phase"] = "COMPLETED"
            ids = [
                row["order_id"]
                for row in self.orders.values()
                if row["robot_id"] == robot_id and row["mode"] == mode and row["status"] == "IN_PROGRESS"
            ]
        for order_id in ids:
            self.cancel_order(order_id, return_to_station=False)

    # /api/robots 조회용으로 로봇별 배차 가능 여부와 막힌 이유를 만든다.
    # 주문 lock은 주문 목록을 복사하는 동안만 짧게 잡고, 못 잡으면 기다리지 않고 None을 돌려준다.
    def dispatch_status(self, mode):
        if not self.lock.acquire(timeout=DISPATCH_STATUS_LOCK_TIMEOUT_S):
            return None
        try:
            assigned = {
                row["robot_id"]: row["order_id"]
                for row in self.orders.values()
                if row["status"] == "IN_PROGRESS" and row["robot_id"]
            }
            recovering = set(self.recovering)
            returning = set(self.returning)
            failed_recovery = {
                row["robot_id"]
                for row in self.orders.values()
                if row["mode"] == mode
                and row["status"] in ("FAILED", "CANCELLED")
                and row.get("recovery_status") in ("BLOCKED", "STOPPED")
            }
        finally:
            self.lock.release()

        # tick()의 busy 계산과 같은 기준을 쓴다.
        busy = set(assigned) | recovering
        held = set()
        if mode == "simulation":
            from .mock_data import mock_fms

            robots = {row["robot_id"]: row for row in mock_fms.robot_snapshots(mode)}
            for robot_id in failed_recovery:
                snapshot = robots.get(robot_id)
                if snapshot and snapshot["occupied_node"] != CHARGING_STATION_NODES.get(robot_id):
                    held.add(robot_id)
            busy |= held
        else:
            robots = {robot.robot_id: robot for robot in fleet_manager.get_all_robots()}
        try:
            available = set(available_robots(mode, busy, returning, mode_manager.operation_mode))
        except ValueError:
            available = set()

        result = {}
        for robot_id, robot in robots.items():
            if scenario_manager.active:
                reason = "scenario_active"
            elif robot_id in available:
                reason = None
            elif robot_id in assigned:
                reason = "assigned_order"
            elif robot_id in recovering:
                reason = "recovering"
            elif robot_id in held:
                reason = "recovery_hold"
            elif mode == "simulation":
                reason = self._simulation_block_reason(robot_id, robot, returning)
            else:
                reason = self._real_block_reason(robot_id, robot, returning)
            result[robot_id] = {
                "assigned_order_id": assigned.get(robot_id),
                "dispatch_available": reason is None,
                "dispatch_block_reason": reason,
            }
        return result

    # 시뮬레이션 로봇이 배차 후보에서 빠진 이유를 order_dispatcher 조건 순서대로 찾는다.
    @staticmethod
    def _simulation_block_reason(robot_id, snapshot, returning):
        if snapshot.get("connection_state") != "ONLINE":
            return "disconnected"
        battery_reason = battery_block_reason(snapshot.get("battery_voltage"))
        if battery_reason is not None:
            return battery_reason
        if (
            robot_id in returning
            and snapshot.get("occupied_node") != CHARGING_STATION_NODES.get(robot_id)
            and snapshot.get("goal_node") is None
        ):
            return "returning_stopped"
        if snapshot.get("occupied_node") is None or snapshot.get("occupied_edge") is not None:
            return "on_edge"
        if snapshot.get("goal_node") is not None or snapshot.get("route") is not None:
            return "navigating"
        if snapshot.get("status") != "IDLE":
            return "not_idle"
        return "unavailable"

    # 실제 로봇이 배차 후보에서 빠진 이유를 order_dispatcher 조건 순서대로 찾는다.
    @staticmethod
    def _real_block_reason(robot_id, robot, returning):
        if not robot.connected:
            return "disconnected"
        if robot.order_id is not None:
            return "robot_order"
        if robot.occupied_node is None:
            return "on_edge"
        if robot.pose_received_at is None or monotonic() - robot.pose_received_at > REAL_POSE_TIMEOUT_S:
            return "stale_pose"
        battery_reason = battery_block_reason(robot.min_battery_voltage())
        if battery_reason is not None:
            return battery_reason
        if (
            robot_id in returning
            and robot.occupied_node != CHARGING_STATION_NODES.get(robot_id)
            and robot.state != RobotState.NAVIGATING
        ):
            return "returning_stopped"
        if robot.goal_node is not None or robot.route is not None or ros_gateway.navigation_active(robot_id):
            return "navigating"
        if robot.state != RobotState.IDLE:
            return "not_idle"
        return "unavailable"

    # 주문 배정을 작업량 기록에 남긴다. 거리를 계산하지 못해도 배정 횟수는 센다. lock 안에서 호출한다.
    def _record_dispatch(self, robot_id, row, mode):
        # 기능이 꺼진 운영에서는 FMS 재시작 전까지 사용할 수 없는 기록을 쌓지 않는다.
        if not _kev_dispatch_recent_workload_enabled():
            return
        distance_m = None
        try:
            profile = self._dispatch_robot_profile(robot_id, mode)
            if profile is not None:
                distance_m = self._pickup_eta(profile, row["pickup_nodes"])["pickup_distance_m"]
        except (OSError, TypeError, ValueError):
            distance_m = None
        self._dispatch_history.append((monotonic(), robot_id, distance_m))
        # KEV를 부르지 않는 배정(후보 1대 등)만 이어져도 기록이 쌓이지 않도록 기간이 지난 기록을 여기서도 지운다.
        self._recent_workload((), _kev_dispatch_workload_window_seconds())

    # 최근 기간 안의 로봇별 배정 횟수와 픽업까지 이동 거리 합을 돌려준다. 기간이 지난 기록은 지운다.
    def _recent_workload(self, robot_ids, window_seconds, now=None):
        now = monotonic() if now is None else now
        while self._dispatch_history and now - self._dispatch_history[0][0] > window_seconds:
            self._dispatch_history.popleft()
        workload = {robot_id: {"recent_dispatch_count": 0, "recent_pickup_distance_m": 0.0} for robot_id in robot_ids}
        for _at, robot_id, distance_m in self._dispatch_history:
            if robot_id not in workload:
                continue
            workload[robot_id]["recent_dispatch_count"] += 1
            if distance_m is not None:
                workload[robot_id]["recent_pickup_distance_m"] += distance_m
        return workload

    # KEV 배차 판단에 사용할 로봇 상태를 만든다.
    def _dispatch_robot_profile(self, robot_id, mode):
        if mode == "simulation":
            from .mock_data import mock_fms

            snapshot = mock_fms.robot_snapshot(robot_id, mode)
            return {
                "robot_id": robot_id,
                "state": snapshot.get("status"),
                "battery": snapshot.get("battery"),
                "current_node": snapshot.get("current_node"),
                "occupied_node": snapshot.get("occupied_node"),
                "goal_node": snapshot.get("goal_node"),
                "returning": robot_id in self.returning,
            }

        robot = fleet_manager.get_robot(robot_id)
        if robot is None:
            return None
        return {
            "robot_id": robot.robot_id,
            "state": robot.state.value if hasattr(robot.state, "value") else str(robot.state),
            "battery": robot.battery,
            "current_node": robot.current_node,
            "occupied_node": robot.occupied_node,
            "goal_node": robot.goal_node,
            "returning": robot_id in self.returning,
        }

    # 대기 주문을 등록 순서대로 정렬한다. KEV 주문 선택 실패와 오래 기다린 주문의 fallback에 쓴다.
    @staticmethod
    def _pending_rows_by_fifo(rows):
        return sorted(rows, key=lambda row: row.get("_queue_sequence", 0))

    # 여러 대기 주문 중 다음 주문을 KEV가 고르도록 요청을 만든다. lock 안에서 호출한다.
    # 시뮬레이션에서 같은 픽업대를 쓰는 앞 주문이 진행 중이면, 복귀 중 로봇이 그 주문 뒤에 끼지 않도록 이 주문 후보에서 뺀다.
    # 배차 루프와 주문 우선순위 KEV가 같은 기준을 쓰도록 함수로 둔다. lock 안에서 호출한다.
    def _blocked_by_earlier_pickup(self, row, mode):
        if mode != "simulation":
            return False
        for previous in self.orders.values():
            if previous is row:
                break
            if (
                previous["mode"] == mode
                and previous["status"] == "IN_PROGRESS"
                and set(previous["pickup_nodes"]).intersection(row["pickup_nodes"])
            ):
                return True
        return False

    # 배차 루프와 같은 기준으로 이 주문에 실제로 배정할 수 있는 로봇 목록. AI 배차의 직전 로봇 제외도 반영한다.
    def _order_dispatch_candidates(self, row, candidates, mode):
        blocked = self._blocked_by_earlier_pickup(row, mode)
        eligible = [candidate for candidate in candidates if not (blocked and candidate in self.returning)]
        last_robot_id = row.get("last_robot_id")
        if last_robot_id in eligible and len(eligible) > 1:
            eligible = [candidate for candidate in eligible if candidate != last_robot_id]
        return eligible

    def _prepare_kev_order_scheduling(self, rows, candidates, mode):
        pending_rows = self._pending_rows_by_fifo(rows)
        if len(pending_rows) < 2 or not candidates:
            return None

        oldest = pending_rows[0]
        oldest_wait_seconds = monotonic() - oldest.get("_queued_at_monotonic", monotonic())
        max_wait_seconds = _kev_order_scheduling_max_wait_seconds()
        # 오래 기다린 주문은 KEV 판단과 무관하게 FIFO로 보장한다. 0이면 이 보호 규칙을 끈다.
        if max_wait_seconds and oldest_wait_seconds >= max_wait_seconds:
            logger.info(
                "KEV order scheduling skipped (FIFO age guard): order=%s waited=%.1fs limit=%.1fs",
                oldest["order_id"],
                oldest_wait_seconds,
                max_wait_seconds,
            )
            return {"kind": "fifo", "order_id": oldest["order_id"]}

        robot_profiles = []
        for robot_id in candidates:
            profile = self._dispatch_robot_profile(robot_id, mode)
            if profile is not None:
                robot_profiles.append(profile)
        if not robot_profiles:
            return {"kind": "fifo", "order_id": oldest["order_id"]}
        profiles_by_id = {profile["robot_id"]: profile for profile in robot_profiles}

        now = monotonic()
        order_profiles = []
        choices = {}
        fifo_order_label = None
        first_dispatchable_order_id = None
        for queue_position, row in enumerate(pending_rows, start=1):
            # 기존 코드. 공통 후보 전체로 ETA와 후보 수를 계산해 주문별 제약(동일 픽업대 대기, 직전 로봇 제외)을 반영하지 못했다.
            # eta_values = []
            # for robot in robot_profiles:
            order_robots = [
                profiles_by_id[robot_id]
                for robot_id in self._order_dispatch_candidates(row, list(profiles_by_id), mode)
            ]
            # 배정할 로봇이 없는 주문은 KEV가 골라도 이번에 배차할 수 없으므로 후보에서 뺀다.
            if not order_robots:
                continue
            if first_dispatchable_order_id is None:
                first_dispatchable_order_id = row["order_id"]
            eta_values = []
            for robot in order_robots:
                try:
                    eta_values.append(self._pickup_eta(robot, row["pickup_nodes"])["pickup_eta_seconds"])
                except (OSError, TypeError, ValueError):
                    continue
            minimum_eta_seconds = min(eta_values) if eta_values else None
            waiting_seconds = max(0.0, now - row.get("_queued_at_monotonic", now))
            # 주문 UUID는 과거 KEV 선택을 흔든 적이 있어 모델 입력과 후보 키에서 제외한다.
            # KEV는 안정적인 큐 라벨만 고르고, 실제 UUID 연결은 아래 order_label_to_id가 코드에서 처리한다.
            order_label = f"order_{queue_position}"
            if row is oldest:
                fifo_order_label = order_label
            profile = {
                "order_label": order_label,
                "queue_position": queue_position,
                "waiting_seconds": waiting_seconds,
                "items": row["items"],
                "total_quantity": row["total_quantity"],
                "pickup_nodes": row["pickup_nodes"],
                # 기존 코드. 공통 후보 수였다.
                # "eligible_robot_count": len(robot_profiles),
                "eligible_robot_count": len(order_robots),
                "minimum_pickup_eta_seconds": minimum_eta_seconds,
            }
            order_profiles.append(profile)
            eta_text = "unknown" if minimum_eta_seconds is None else f"{minimum_eta_seconds:.3f}"
            choices[order_label] = (
                f"queue_position={queue_position}, "
                f"waiting_seconds={waiting_seconds:.3f}, "
                f"total_quantity={row['total_quantity']}, "
                f"pickup_nodes={row['pickup_nodes']}, "
                f"eligible_robot_count={len(order_robots)}, "
                f"minimum_pickup_eta_seconds={eta_text}"
            )

        # 배정할 로봇이 있는 주문이 하나뿐이면 KEV로 고를 것이 없다.
        if len(choices) < 2:
            return {"kind": "fifo", "order_id": first_dispatchable_order_id or oldest["order_id"]}
        # 기존 코드. 배정할 로봇이 없는 주문을 후보에서 빼므로 제시한 라벨만 연결한다.
        # order_label_to_id = {
        #     f"order_{queue_position}": row["order_id"] for queue_position, row in enumerate(pending_rows, start=1)
        # }
        order_label_to_id = {
            f"order_{queue_position}": row["order_id"]
            for queue_position, row in enumerate(pending_rows, start=1)
            if f"order_{queue_position}" in choices
        }
        question_id = f"order_schedule_{pending_rows[0]['_queue_sequence']}"
        instructions = (
            "Choose exactly one listed pending order label to dispatch next. "
            "Do not select a label outside the candidates. Prefer the order waiting longest when wait times differ materially. "
            "When waiting times are similar, prefer the smaller minimum_pickup_eta_seconds to reduce immediate idle travel. "
            "Do not infer urgency, deadlines, safety, or capacity constraints that are not provided. Return only one candidate label."
        )
        debug_record = {
            "question_id": question_id,
            # 기존 코드. 가장 오래된 주문이 후보에서 빠질 수 있어 실제 라벨을 넣는다.
            # "fifo_order_label": "order_1",
            "fifo_order_label": fifo_order_label,
            "state": {
                "pending_orders": order_profiles,
                "scheduling_context": {
                    "eligible_robot_ids": [profile["robot_id"] for profile in robot_profiles],
                    "max_wait_seconds": max_wait_seconds,
                },
            },
            "choices": choices,
            "instructions": instructions,
        }
        return {
            "kind": "request",
            "request_type": "order_scheduling",
            "candidate_labels": list(choices),
            "order_label_to_id": order_label_to_id,
            "fifo_order_id": oldest["order_id"],
            "debug_record": debug_record,
        }

    # 배차 가능한 후보 중 한 대를 KEV로 선택한다.
    # tick은 lock 밖에서 KEV를 부르기 위해 준비(_prepare_kev_dispatch)와 호출(_request_kev_dispatch)을 따로 쓴다.
    def _select_robot_with_kev(self, row, candidates, mode):
        kind, value = self._prepare_kev_dispatch(row, candidates, mode)
        if kind == "robot":
            return value
        return self._request_kev_dispatch(value)

    # 한 로봇이 주문에서 실제로 첫 방문할 픽업 노드에 도달하는 최단 거리와 ETA를 계산한다.
    @staticmethod
    def _pickup_eta(profile, pickup_nodes):
        current_node = profile.get("current_node")
        if current_node is None:
            raise ValueError(f"현재 노드가 없는 로봇입니다: {profile['robot_id']}")

        if not pickup_nodes:
            raise ValueError("주문에 픽업 노드가 없습니다.")
        # 기존 코드. 모든 픽업 노드 중 가장 가까운 곳을 ETA 기준으로 골랐다.
        # routes = []
        # for pickup_node in pickup_nodes:
        #     route = plan_route(str(current_node), str(pickup_node))
        #     routes.append((float(route["cost"]), str(pickup_node)))
        # distance_m, pickup_node = min(routes)

        # 실제 주문 이동은 pickup_nodes 순서대로 진행한다. 복합 주문도 첫 방문 노드까지의 ETA로 배차한다.
        pickup_node = str(pickup_nodes[0])
        route = plan_route(str(current_node), pickup_node)
        distance_m = float(route["cost"])
        return {
            "pickup_node": pickup_node,
            "pickup_distance_m": distance_m,
            "pickup_eta_seconds": distance_m / _kev_dispatch_eta_speed_mps(),
        }

    @staticmethod
    def _select_robot_by_dispatch_rules(profiles):
        """Choose the temporary code baseline inside one ETA near-tie group."""

        # 임시 비교 기준선: 작업량·작업 후 예상 배터리 정책이 확정되기 전까지만 사용한다.
        # 우선순위는 IDLE·비복귀, 현재 배터리 높은 순, robot_id 오름차순이다.
        # battery가 None, 비수치, NaN이면 유효한 배터리 값보다 뒤로 보낸다.
        def priority(profile):
            state = profile.get("state")
            idle_and_not_returning = state == RobotState.IDLE.value and not profile.get("returning")
            try:
                battery = float(profile.get("battery"))
            except (TypeError, ValueError):
                battery = None
            if battery is None or not math.isfinite(battery):
                battery_priority = (1, 0.0)
            else:
                battery_priority = (0, -battery)
            return (0 if idle_and_not_returning else 1, *battery_priority, str(profile["robot_id"]))

        if not profiles:
            return None
        return min(profiles, key=priority)["robot_id"]

    # KEV 호출 전에 후보를 정리한다. ("robot", 로봇 ID 또는 None) 또는 ("request", 요청)을 반환한다.
    # lock 안에서 호출한다.
    def _prepare_kev_dispatch(self, row, candidates, mode):
        if not candidates:
            return "robot", None
        # 재시도 주문은 다른 후보가 있으면 직전 로봇을 KEV 후보에서 뺀다.
        # V1 지시문의 "Avoid the last robot"을 지시문 대신 코드 규칙으로 옮긴 것이다.
        last_robot_id = row.get("last_robot_id")
        excluded_last_robot = None
        if last_robot_id in candidates and len(candidates) > 1:
            candidates = [candidate for candidate in candidates if candidate != last_robot_id]
            excluded_last_robot = last_robot_id
        # Kev choice API는 후보가 두 개 이상이어야 한다.
        if len(candidates) == 1:
            # 기존 로그. 직전 로봇 제외 여부를 함께 남기도록 바꾼다.
            # logger.info("KEV dispatch skipped (single candidate): robot=%s order=%s", candidates[0], row["order_id"])
            logger.info(
                "KEV dispatch skipped (single candidate): robot=%s order=%s excluded_last_robot=%s",
                candidates[0],
                row["order_id"],
                excluded_last_robot,
            )
            return "robot", candidates[0]

        profiles = []
        for robot_id in candidates:
            profile = self._dispatch_robot_profile(robot_id, mode)
            if profile is not None:
                profiles.append(profile)
        if not profiles:
            return "robot", None
        # 후보 범위 오타는 ETA 계산 실패와 달리 실험 설정 오류다. 기존 알고리즘으로 조용히 fallback하지 않는다.
        candidate_scope = _kev_dispatch_candidate_scope()
        recent_workload_enabled = _kev_dispatch_recent_workload_enabled()
        workload_window_seconds = _kev_dispatch_workload_window_seconds() if recent_workload_enabled else None
        try:
            eta_tie_window_seconds = _kev_dispatch_eta_tie_seconds()
            for profile in profiles:
                profile.update(self._pickup_eta(profile, row["pickup_nodes"]))
        except (OSError, TypeError, ValueError) as exc:
            # ETA를 계산할 수 없으면 안전하게 기존 알고리즘 fallback을 사용한다.
            logger.warning("KEV dispatch ETA preparation failed; falling back to algorithm: order=%s error=%r", row["order_id"], exc)
            return "robot", None

        profiles.sort(key=lambda profile: profile["robot_id"])
        minimum_eta_seconds = min(profile["pickup_eta_seconds"] for profile in profiles)
        near_eta_profiles = [
            profile
            for profile in profiles
            if profile["pickup_eta_seconds"] <= minimum_eta_seconds + eta_tie_window_seconds
        ]
        excluded_by_eta = [
            profile["robot_id"] for profile in profiles if profile not in near_eta_profiles
        ]
        # 기존 코드. all_eligible에서는 근소 범위 밖 후보도 KEV에 보내므로 모든 후보에 차이를 넣는다.
        # for profile in near_eta_profiles:
        #     profile["eta_delta_seconds"] = profile["pickup_eta_seconds"] - minimum_eta_seconds
        for profile in profiles:
            profile["eta_delta_seconds"] = profile["pickup_eta_seconds"] - minimum_eta_seconds
        # robot_id 순으로 정렬했으므로 ETA가 같으면 앞 ID가 최단 ETA 로봇이다. KEV가 최단 ETA를 골랐는지 기록할 때 쓴다.
        eta_winner = min(profiles, key=lambda profile: profile["pickup_eta_seconds"])["robot_id"]

        # 기존 코드. all_eligible에서는 ETA가 확실히 짧아도 KEV가 판단하도록 바꾼다.
        # if len(near_eta_profiles) == 1:
        if candidate_scope == "near_eta" and len(near_eta_profiles) == 1:
            robot_id = near_eta_profiles[0]["robot_id"]
            logger.info(
                "KEV dispatch skipped (ETA winner): robot=%s order=%s eta=%.3fs tie_window=%.3fs excluded=%s",
                robot_id,
                row["order_id"],
                minimum_eta_seconds,
                eta_tie_window_seconds,
                excluded_by_eta,
            )
            return "robot", robot_id

        # 기존 코드. 기준선은 두 범위 모두 근소 후보군에서 고르고, KEV 후보만 범위에 따라 바꾼다.
        # profiles = near_eta_profiles
        # candidates = [profile["robot_id"] for profile in profiles]
        # if len(profiles) == 1:
        #     ...
        # rule_baseline_choice = self._select_robot_by_dispatch_rules(profiles)
        # KEV 실패·cooldown fallback은 범위와 관계없이 같은 ETA 우선 기준선을 써서 두 방식을 같은 조건에서 비교한다.
        rule_baseline_choice = self._select_robot_by_dispatch_rules(near_eta_profiles)
        if candidate_scope == "near_eta":
            profiles = near_eta_profiles
        else:
            excluded_by_eta = []
        candidates = [profile["robot_id"] for profile in profiles]
        if len(profiles) == 1:
            logger.info(
                "KEV dispatch skipped (single profile): robot=%s order=%s", profiles[0]["robot_id"], row["order_id"]
            )
            return "robot", profiles[0]["robot_id"]

        # 최근 작업량은 켰을 때만 KEV 입력에 넣는다. 끄면 기존 실험과 같은 입력이다.
        if recent_workload_enabled:
            workload = self._recent_workload([profile["robot_id"] for profile in profiles], workload_window_seconds)
            for profile in profiles:
                profile["recent_dispatch_count"] = workload[profile["robot_id"]]["recent_dispatch_count"]
                profile["recent_pickup_distance_m"] = round(workload[profile["robot_id"]]["recent_pickup_distance_m"], 3)
        choices = {
            profile["robot_id"]: (
                f"state={profile['state']}, "
                f"battery={profile['battery']}, "
                f"current_node={profile['current_node']}, "
                f"occupied_node={profile['occupied_node']}, "
                f"goal_node={profile['goal_node']}, "
                f"returning={profile['returning']}, "
                f"pickup_node={profile['pickup_node']}, "
                f"pickup_distance_m={profile['pickup_distance_m']:.3f}, "
                f"pickup_eta_seconds={profile['pickup_eta_seconds']:.3f}, "
                f"eta_delta_seconds={profile['eta_delta_seconds']:.3f}"
                + (
                    f", recent_dispatch_count={profile['recent_dispatch_count']}, "
                    f"recent_pickup_distance_m={profile['recent_pickup_distance_m']:.3f}"
                    if recent_workload_enabled
                    else ""
                )
            )
            for profile in profiles
        }
        state = {
            "order": {
                # 기존 코드. 직접 재생 실험(D-3)에서 order_id가 선택을 흔들어 state에서는 뺀다.
                # "order_id": row["order_id"],
                "items": row["items"],
                "total_quantity": row["total_quantity"],
                "pickup_nodes": row["pickup_nodes"],
                "workstation_node": row.get("workstation_node"),
                "last_robot_id": row.get("last_robot_id"),
            },
            "robots": profiles,
            "dispatch_context": {
                # 기존 코드. all_eligible에서는 ETA 필터를 쓰지 않는다.
                # "eta_filter_applied": True,
                # near_eta의 KEV 입력은 기존 실험(D-6·D-7)과 같게 두고, 범위는 디버그 기록에만 남긴다.
                "eta_filter_applied": candidate_scope == "near_eta",
                "minimum_pickup_eta_seconds": minimum_eta_seconds,
                "eta_tie_window_seconds": eta_tie_window_seconds,
                "excluded_by_eta": excluded_by_eta,
                "post_task_battery_available": False,
                # 기존 코드. 최근 작업량을 켜면 True와 집계 기간을 넣는다.
                # "recent_workload_available": False,
                "recent_workload_available": recent_workload_enabled,
            },
        }
        if recent_workload_enabled:
            state["dispatch_context"]["recent_workload_window_seconds"] = workload_window_seconds
        # 기존 지시문(V1). D-3에서 최고 배터리 선택 정확도가 2/6이어서 V3로 바꾼다.
        # instructions = (
        #     "Choose exactly one available robot for this order. "
        #     "Prefer an idle and non-returning robot. "
        #     "Prefer higher battery when other conditions are similar. "
        #     "Avoid the last robot when another suitable robot is available. "
        #     "Return only one candidate ID."
        # )
        # 수정 전 V3 지시문. 배터리를 최우선으로 선택했으나 ETA 우선 정책과 맞지 않아 교체한다.
        # instructions = (
        #     "Among idle and non-returning robots, choose the one with the highest battery. "
        #     "Use other fields only to break exact ties. Return only one candidate ID."
        # )
        # near_eta 지시문. all_eligible은 아래에서 따로 쓴다.
        instructions = (
            "All listed robots have passed code safety checks and are within the configured pickup ETA near-tie window. "
            "Choose exactly one listed candidate ID. Do not re-rank candidates by pickup ETA and do not treat battery as a safety threshold. "
            "Prefer an IDLE, non-returning robot when operational state differs. Use the current battery only as secondary context; "
            "it is not an estimated post-task battery. Return only one candidate ID."
        )
        if candidate_scope == "all_eligible":
            instructions = (
                "All listed robots have passed code safety checks, but they are not filtered by pickup ETA. "
                "Choose exactly one listed candidate ID. Pickup ETA is the primary criterion: prefer the shortest "
                "pickup_eta_seconds. When eta_delta_seconds differences are small, prefer an IDLE, non-returning robot. "
                "Use the current battery only as secondary context; it is not a safety threshold or an estimated "
                "post-task battery. Return only one candidate ID."
            )
        if recent_workload_enabled:
            instructions += (
                " recent_dispatch_count and recent_pickup_distance_m are this robot's assignments and estimated pickup "
                "travel within recent_workload_window_seconds. When pickup ETA and operational state are similar, prefer "
                "the robot with less recent workload to balance assignments; do not let workload override a clearly "
                "shorter pickup ETA."
            )
        # 기존 질문 ID. UUID는 선택 입력에 불필요하고 실험마다 달라져 재현성을 흐릴 수 있다.
        # question_id = f"dispatch_{row['order_id']}"
        question_id = f"dispatch_order_{row.get('_queue_sequence', 0)}_retry_{row.get('retry_count', 0)}"
        debug_record = {
            "order_id": row["order_id"],
            "question_id": question_id,
            "excluded_last_robot": excluded_last_robot,
            # KEV 입력에는 넣지 않는다. 비교 기록과 KEV 오류 시 코드 fallback에만 쓴다.
            "rule_baseline_choice": rule_baseline_choice,
            "candidate_scope": candidate_scope,
            "eta_winner": eta_winner,
            "state": state,
            "choices": choices,
            "instructions": instructions,
        }
        # state, choices는 lock 안에서 만든 사본이라 lock 밖에서 읽어도 된다.
        return "request", {
            "order_id": row["order_id"],
            "candidates": list(candidates),
            "rule_baseline_choice": rule_baseline_choice,
            "candidate_scope": candidate_scope,
            "eta_winner": eta_winner,
            # all_eligible은 KEV가 근소 범위 밖 후보를 고를 수 있으므로 같은 범위(초)를 상한으로 다시 검증한다.
            # near_eta는 후보가 이미 범위 안이라 검증하지 않는다.
            "max_eta_delta_seconds": eta_tie_window_seconds if candidate_scope == "all_eligible" else None,
            "eta_delta_by_robot": {profile["robot_id"]: profile["eta_delta_seconds"] for profile in profiles},
            "debug_record": debug_record,
        }

    # 준비한 요청으로 KEV를 호출하고 선택된 로봇 ID를 반환한다. 공유 상태를 건드리지 않으므로 lock 밖에서 호출한다.
    def _request_kev_dispatch(self, request):
        if self._kev_dispatch_selector is None:
            from .route_selector.kev_selector import KevSelector

            # 기존 코드. 기본 KEV_TIMEOUT_SECONDS(60초)를 그대로 써서 tick이 오래 멈출 수 있었다.
            # self._kev_dispatch_selector = KevSelector()
            # KEV_MODEL, KEV_HOST 등은 환경변수에서 읽는다. 배차 timeout만 경로 선택과 따로 둔다.
            self._kev_dispatch_selector = KevSelector(timeout_seconds=KEV_DISPATCH_TIMEOUT_SECONDS)

        debug_record = request["debug_record"]
        candidates = request["candidates"]
        debug_enabled = os.getenv("KEV_DISPATCH_DEBUG") == "1"
        try:
            result = self._kev_dispatch_selector.select_choice(
                state=debug_record["state"],
                candidates=debug_record["choices"],
                instructions=debug_record["instructions"],
                question_id=debug_record["question_id"],
            )
        except Exception as exc:
            if debug_enabled:
                logger.info(
                    "KEV_DISPATCH_DEBUG %s",
                    json.dumps(
                        {
                            **debug_record,
                            "response": None,
                            "error": {"type": type(exc).__name__, "message": str(exc)},
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            raise

        if debug_enabled:
            logger.info(
                "KEV_DISPATCH_DEBUG %s",
                json.dumps(
                    {
                        **debug_record,
                        "response": {
                            "probabilities": result.get("probabilities"),
                            "confidence": result.get("confidence"),
                            "answer_confidence": result.get("answer_confidence"),
                            "choice": result.get("choice"),
                        },
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        chosen_robot = result.get("choice")
        if chosen_robot not in candidates:
            raise RuntimeError(f"KEV가 후보 목록에 없는 로봇을 선택했습니다: {chosen_robot}")
        max_eta_delta_seconds = request.get("max_eta_delta_seconds")
        if max_eta_delta_seconds is not None:
            eta_delta_seconds = request["eta_delta_by_robot"][chosen_robot]
            if eta_delta_seconds > max_eta_delta_seconds + 1e-9:
                logger.warning(
                    "KEV dispatch rejected (ETA guard): robot=%s order=%s eta_delta=%.3fs limit=%.3fs eta_winner=%s",
                    chosen_robot,
                    request["order_id"],
                    eta_delta_seconds,
                    max_eta_delta_seconds,
                    request.get("eta_winner"),
                )
                raise KevDispatchEtaRejected(
                    f"KEV 선택 {chosen_robot}의 ETA가 최단보다 {eta_delta_seconds:.3f}초 길어 상한 "
                    f"{max_eta_delta_seconds:.3f}초를 넘었습니다."
                )
        # 기존 로그. 후보 범위와 최단 ETA 로봇을 함께 남기도록 바꾼다.
        # logger.info(
        #     "KEV dispatch selected robot=%s order=%s candidates=%s rule_baseline=%s",
        #     chosen_robot,
        #     request["order_id"],
        #     candidates,
        #     request["rule_baseline_choice"],
        # )
        logger.info(
            "KEV dispatch selected robot=%s order=%s candidates=%s rule_baseline=%s scope=%s eta_winner=%s",
            chosen_robot,
            request["order_id"],
            candidates,
            request["rule_baseline_choice"],
            request.get("candidate_scope", "near_eta"),
            request.get("eta_winner"),
        )
        return chosen_robot

    # 준비한 요청으로 KEV에게 다음 대기 주문을 선택하게 한다. 공유 상태를 건드리지 않으므로 lock 밖에서 호출한다.
    def _request_kev_order_scheduling(self, request):
        if self._kev_dispatch_selector is None:
            from .route_selector.kev_selector import KevSelector

            self._kev_dispatch_selector = KevSelector(timeout_seconds=KEV_DISPATCH_TIMEOUT_SECONDS)

        debug_record = request["debug_record"]
        candidate_labels = request["candidate_labels"]
        debug_enabled = os.getenv("KEV_DISPATCH_DEBUG") == "1"
        try:
            result = self._kev_dispatch_selector.select_choice(
                state=debug_record["state"],
                candidates=debug_record["choices"],
                instructions=debug_record["instructions"],
                question_id=debug_record["question_id"],
            )
        except Exception as exc:
            if debug_enabled:
                logger.info(
                    "KEV_ORDER_SCHEDULING_DEBUG %s",
                    json.dumps(
                        {
                            **debug_record,
                            "response": None,
                            "error": {"type": type(exc).__name__, "message": str(exc)},
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            raise

        if debug_enabled:
            logger.info(
                "KEV_ORDER_SCHEDULING_DEBUG %s",
                json.dumps(
                    {
                        **debug_record,
                        "response": {
                            "probabilities": result.get("probabilities"),
                            "confidence": result.get("confidence"),
                            "answer_confidence": result.get("answer_confidence"),
                            "choice": result.get("choice"),
                        },
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        chosen_label = result.get("choice")
        if chosen_label not in candidate_labels:
            raise RuntimeError(f"KEV가 후보 목록에 없는 주문 라벨을 선택했습니다: {chosen_label}")
        chosen_order_id = request["order_label_to_id"][chosen_label]
        logger.info(
            "KEV order scheduling selected order=%s label=%s candidate_labels=%s fifo_order=%s",
            chosen_order_id,
            chosen_label,
            candidate_labels,
            request["fifo_order_id"],
        )
        return chosen_order_id

    # tick에서 KEV 요청을 별도 스레드로 보내고, 예외도 결과로 돌려준다.
    async def _run_kev_dispatch(self, request):
        result = {"request_type": "robot_dispatch", "order_id": request["order_id"], "candidates": request["candidates"]}
        try:
            result["choice"] = await asyncio.to_thread(self._request_kev_dispatch, request)
        except Exception as exc:
            result["error"] = exc
        return result

    # 주문 우선순위 KEV 요청도 로봇 배차와 마찬가지로 lock 밖 스레드에서 실행한다.
    async def _run_kev_order_scheduling(self, request):
        result = {
            "request_type": "order_scheduling",
            "candidate_order_ids": list(request["order_label_to_id"].values()),
            "fifo_order_id": request["fifo_order_id"],
        }
        try:
            result["choice"] = await asyncio.to_thread(self._request_kev_order_scheduling, request)
        except Exception as exc:
            result["error"] = exc
        return result

    # 현재 규칙 기반 우선순위로 로봇을 선택한다. KEV 실패 시에도 사용한다.
    def _select_robot_by_algorithm(self, row, candidates, blocked_by_earlier_pickup):
        preferred = sorted(enumerate(candidates), key=lambda item: item[1] == row.get("last_robot_id"))
        for index, candidate in preferred:
            if blocked_by_earlier_pickup and candidate in self.returning:
                continue
            return candidates.pop(index)
        return None

    # 대기 중인 작업을 한 주기 진행한다.
    async def tick(self):
        if scenario_manager.active:
            return
        mode = mode_manager.mode
        preemptions = []
        # 기존 코드는 아래 _dispatch_round의 lock 블록을 tick 안에서 바로 실행했고, KEV HTTP 호출도 lock 안에서 했다.
        # 이제 KEV가 필요하면 lock을 풀고 스레드에서 호출한 뒤, 결과를 들고 배차를 다시 시도한다.
        kev_result = None
        scheduled_order_id = None
        for _ in range(KEV_DISPATCH_MAX_ROUNDS):
            kev_request, active, scheduled_order_id = self._dispatch_round(
                mode, preemptions, kev_result, scheduled_order_id
            )
            if kev_request is None:
                break
            if kev_request.get("request_type") == "order_scheduling":
                kev_result = await self._run_kev_order_scheduling(kev_request)
            else:
                kev_result = await self._run_kev_dispatch(kev_request)
        if mode == "real":
            for order_id, robot_id in preemptions:
                try:
                    ros_gateway.cancel_current_work(
                        robot_id, lambda oid=order_id, rid=robot_id: self._start_after_return_stop(oid, rid)
                    )
                except (ValueError, RuntimeError) as exc:
                    self.fail(order_id, str(exc))
            for row in active:
                await self._tick_real(row)
            await self._tick_real_returns()

    # 대기 주문 배차를 lock 안에서 한 번 진행한다.
    # KEV 호출이 필요한 주문을 만나면 그 주문에서 멈추고 (KEV 요청, 진행 중 주문)을 반환한다.
    # kev_result는 직전 KEV 호출 결과이며, 같은 주문과 같은 후보일 때만 사용한다.
    def _dispatch_round(self, mode, preemptions, kev_result, scheduled_order_id):
        kev_request = None
        with self.lock:
            busy = {
                row["robot_id"] for row in self.orders.values() if row["status"] == "IN_PROGRESS" and row["robot_id"]
            }
            busy.update(self.recovering)
            if mode == "simulation":
                from .mock_data import mock_fms
                from .simulation_order_runner import simulation_order_runner

                # A failed recovery must not silently become a new dispatch.
                # Manual return to the station makes the robot available again.
                for row in self.orders.values():
                    if (
                        row["mode"] == mode
                        and row["status"] in ("FAILED", "CANCELLED")
                        and row.get("recovery_status") in ("BLOCKED", "STOPPED")
                    ):
                        snapshot = mock_fms.robot_snapshot(row["robot_id"], mode)
                        if snapshot["occupied_node"] != CHARGING_STATION_NODES.get(row["robot_id"]):
                            busy.add(row["robot_id"])
                        elif (
                            snapshot["goal_node"] is None
                            and snapshot["route"] is None
                            and not snapshot.get("stop_requested")
                        ):
                            row["recovery_status"] = "RETURNED"
                for robot_id, failed_id in self.recovering.items():
                    simulation_order_runner.start_recovery(failed_id, robot_id)
            candidates = available_robots(mode, busy, set(self.returning), mode_manager.operation_mode)
            # 기존 FIFO 순회 코드. 주문 우선순위 KEV가 꺼져 있으면 아래 pending_rows가 같은 등록 순서를 유지한다.
            # for row in self.orders.values():
            #     if row["mode"] != mode or row["status"] != "PENDING" or not candidates:
            #         continue
            pending_rows = self._pending_rows_by_fifo(
                [row for row in self.orders.values() if row["mode"] == mode and row["status"] == "PENDING"]
            )

            # AI 주문 우선순위는 한 tick에서 한 번만 결정한다. 이후 로봇 KEV 응답을 받으러 다시 들어와도
            # 같은 주문을 맨 앞으로 유지해 KEV 호출이 반복되지 않게 한다.
            scheduling_enabled = (
                bool(candidates)
                and mode_manager.operation_mode == "ai"
                and _kev_order_scheduling_enabled()
            )
            if scheduling_enabled and scheduled_order_id is None:
                if kev_result is not None and kev_result.get("request_type") == "order_scheduling":
                    valid_order_ids = {row["order_id"] for row in pending_rows}
                    if "error" in kev_result:
                        self._kev_order_scheduling_retry_at = monotonic() + KEV_DISPATCH_COOLDOWN_SECONDS
                        logger.warning(
                            "KEV order scheduling failed; falling back to FIFO for %.0fs: error=%r",
                            KEV_DISPATCH_COOLDOWN_SECONDS,
                            kev_result["error"],
                            exc_info=kev_result["error"],
                        )
                        scheduled_order_id = kev_result["fifo_order_id"]
                    elif kev_result.get("choice") in valid_order_ids:
                        scheduled_order_id = kev_result["choice"]
                    else:
                        logger.info(
                            "KEV order scheduling result discarded (pending orders changed): asked=%s now=%s",
                            kev_result.get("candidate_order_ids"),
                            sorted(valid_order_ids),
                        )
                        scheduled_order_id = pending_rows[0]["order_id"] if pending_rows else None
                elif monotonic() >= self._kev_order_scheduling_retry_at:
                    scheduling = self._prepare_kev_order_scheduling(pending_rows, candidates, mode)
                    if scheduling is not None:
                        if scheduling["kind"] == "request":
                            active = [
                                row for row in self.orders.values() if row["mode"] == mode and row["status"] == "IN_PROGRESS"
                            ]
                            return scheduling, active, scheduled_order_id
                        scheduled_order_id = scheduling["order_id"]
                else:
                    logger.info(
                        "KEV order scheduling skipped (cooldown %.1fs left); using FIFO",
                        self._kev_order_scheduling_retry_at - monotonic(),
                    )
                    scheduled_order_id = pending_rows[0]["order_id"] if pending_rows else None

            if scheduled_order_id is not None:
                pending_rows.sort(key=lambda row: (row["order_id"] != scheduled_order_id, row.get("_queue_sequence", 0)))

            for row in pending_rows:
                if not candidates:
                    break
                # 기존 코드. 주문 우선순위 KEV와 같은 기준을 쓰도록 _blocked_by_earlier_pickup으로 옮겼다.
                # blocked_by_earlier_pickup = False
                # if mode == "simulation":
                #     for previous in self.orders.values():
                #         if previous is row:
                #             break
                #         if (
                #             previous["mode"] == mode
                #             and previous["status"] == "IN_PROGRESS"
                #             and set(previous["pickup_nodes"]).intersection(row["pickup_nodes"])
                #         ):
                #             blocked_by_earlier_pickup = True
                #             break
                blocked_by_earlier_pickup = self._blocked_by_earlier_pickup(row, mode)

                # 기존 알고리즘 배차 코드. KEV fallback에서 같은 정책을 함수로 호출한다.
                # candidate_index = None
                # preferred = sorted(enumerate(candidates), key=lambda item: item[1] == row.get("last_robot_id"))
                # for index, candidate in preferred:
                #     # A returning robot must leave the workstation before
                #     # waiting behind an earlier order at the same pickup.
                #     if blocked_by_earlier_pickup and candidate in self.returning:
                #         continue
                #     candidate_index = index
                #     break
                # if candidate_index is None:
                #     continue
                # robot_id = candidates.pop(candidate_index)

                eligible_candidates = [
                    candidate
                    for candidate in candidates
                    if not (blocked_by_earlier_pickup and candidate in self.returning)
                ]
                if not eligible_candidates:
                    continue

                robot_id = None
                dispatch_engine = feature_engine("dispatch", row["operation_mode"])
                fallback_candidates = candidates
                rule_baseline_choice = None
                # 기존 KEV 분기. 실패해도 매 tick 다시 호출했고, 후보가 1대여도 "selected" 로그를 남겼다.
                # if dispatch_engine == "ai":
                #     try:
                #         robot_id = self._select_robot_with_kev(row, eligible_candidates, mode)
                #         if robot_id is not None:
                #             candidates.remove(robot_id)
                #             logger.info(
                #                 "KEV dispatch selected robot=%s order=%s candidates=%s",
                #                 robot_id,
                #                 row["order_id"],
                #                 eligible_candidates,
                #             )
                #     except Exception as exc:
                #         # KEV 서버 오류나 잘못된 응답은 주문 실패가 아니라 기존 배차로 복구한다.
                #         logger.warning(
                #             "KEV dispatch failed; falling back to algorithm: order=%s error=%r",
                #             row["order_id"],
                #             exc,
                #         )
                #         robot_id = None
                # 기존 KEV 분기. lock 안에서 KEV HTTP를 동기로 호출해 응답을 기다리는 동안 lock과 이벤트 루프가 멈췄다.
                # if dispatch_engine == "ai" and monotonic() >= self._kev_dispatch_retry_at:
                #     try:
                #         robot_id = self._select_robot_with_kev(row, eligible_candidates, mode)
                #         if robot_id is not None:
                #             candidates.remove(robot_id)
                #     except Exception as exc:
                #         # KEV 서버 오류나 잘못된 응답은 주문 실패가 아니라 기존 배차로 복구한다.
                #         # 실패가 반복되면 tick마다 timeout을 기다리지 않도록 잠시 KEV 호출을 멈춘다.
                #         self._kev_dispatch_retry_at = monotonic() + KEV_DISPATCH_COOLDOWN_SECONDS
                #         logger.warning(
                #             "KEV dispatch failed; falling back to algorithm for %.0fs: order=%s error=%r",
                #             KEV_DISPATCH_COOLDOWN_SECONDS,
                #             row["order_id"],
                #             exc,
                #             exc_info=True,
                #         )
                #         robot_id = None
                # elif dispatch_engine == "ai":
                kev_failure = None
                is_robot_dispatch_result = kev_result is not None and kev_result.get("request_type") == "robot_dispatch"
                if is_robot_dispatch_result and kev_result["order_id"] == row["order_id"] and "error" in kev_result:
                    kev_failure = kev_result.pop("error")
                if dispatch_engine == "ai":
                    kind, value = self._prepare_kev_dispatch(row, eligible_candidates, mode)
                    if kind == "robot":
                        robot_id = value
                    else:
                        # KEV 실패·cooldown에서도 ETA 정책은 유지하기 위해 같은 후보군으로 fallback한다.
                        fallback_candidates = [candidate for candidate in candidates if candidate in value["candidates"]]
                        rule_baseline_choice = value["rule_baseline_choice"]
                        if isinstance(kev_failure, KevDispatchEtaRejected):
                            # 정상 응답이지만 ETA 정책을 어긴 선택이다. 서버 문제가 아니므로 cooldown 없이 기준선으로 배정한다.
                            logger.warning(
                                "KEV dispatch falling back to rule baseline (ETA guard): order=%s baseline=%s error=%s",
                                row["order_id"],
                                rule_baseline_choice,
                                kev_failure,
                            )
                        elif kev_failure is not None:
                            # KEV 서버 오류나 잘못된 응답은 주문 실패가 아니라 ETA 후보군 안의 기존 배차로 복구한다.
                            # 실패가 반복되면 tick마다 timeout을 기다리지 않도록 잠시 KEV 호출을 멈춘다.
                            self._kev_dispatch_retry_at = monotonic() + KEV_DISPATCH_COOLDOWN_SECONDS
                            logger.warning(
                                "KEV dispatch failed; falling back to algorithm for %.0fs: order=%s error=%r",
                                KEV_DISPATCH_COOLDOWN_SECONDS,
                                row["order_id"],
                                kev_failure,
                                exc_info=kev_failure,
                            )
                        elif monotonic() >= self._kev_dispatch_retry_at:
                            if (
                                is_robot_dispatch_result
                                and kev_result["order_id"] == row["order_id"]
                                and kev_result["candidates"] == value["candidates"]
                            ):
                                robot_id = kev_result["choice"]
                            else:
                                if is_robot_dispatch_result and kev_result["order_id"] == row["order_id"]:
                                    # KEV 응답을 기다리는 동안 후보가 바뀌었으면 결과를 버리고 다시 묻는다.
                                    logger.info(
                                        "KEV dispatch result discarded (candidates changed): order=%s asked=%s now=%s",
                                        row["order_id"],
                                        kev_result["candidates"],
                                        value["candidates"],
                                    )
                                # 앞 주문이 먼저 로봇을 받도록 뒤 주문은 이번 차례에 배차하지 않는다.
                                kev_request = value
                                break
                        else:
                            logger.info(
                                "KEV dispatch skipped (cooldown %.1fs left): order=%s",
                                self._kev_dispatch_retry_at - monotonic(),
                                row["order_id"],
                            )
                    if robot_id is not None:
                        candidates.remove(robot_id)

                if robot_id is None:
                    if rule_baseline_choice in fallback_candidates:
                        robot_id = rule_baseline_choice
                        fallback_candidates.remove(robot_id)
                    else:
                        robot_id = self._select_robot_by_algorithm(row, fallback_candidates, blocked_by_earlier_pickup)
                    if robot_id is not None and fallback_candidates is not candidates and robot_id in candidates:
                        candidates.remove(robot_id)
                if robot_id is None:
                    continue
                self._record_dispatch(robot_id, row, mode)
                row["robot_id"] = robot_id
                row["status"] = "IN_PROGRESS"
                returning = robot_id in self.returning
                row["phase"] = "WAITING_RETURN_STOP" if mode == "real" and returning else "MOVING_TO_PICKUP"
                row["route_started"] = False
                busy.add(robot_id)
                previous_order_id = self.returning.pop(robot_id, None)
                if previous_order_id in self.orders:
                    self.orders[previous_order_id]["phase"] = "COMPLETED"
                self._return_started.discard(robot_id)
                if mode == "simulation":
                    from .mock_data import mock_fms

                    snapshot = mock_fms.robot_snapshot(robot_id, mode)
                    mock_fms.stop_robot(robot_id)
                    row["_held_node"] = snapshot["occupied_node"]
                    from .simulation_order_runner import simulation_order_runner

                    simulation_order_runner.start(row["order_id"])
                else:
                    if returning:
                        preemptions.append((row["order_id"], robot_id))
                    else:
                        robot = fleet_manager.get_robot(robot_id)
                        robot.assign_order(
                            row["order_id"], row["items"], row["total_quantity"], row["pickup_nodes"], None
                        )
            active = [row for row in self.orders.values() if row["mode"] == mode and row["status"] == "IN_PROGRESS"]
        # 기존 코드. 실제 모드 후처리는 KEV 재시도가 끝난 뒤 한 번만 하도록 tick으로 옮겼다.
        # if mode == "real":
        #     for order_id, robot_id in preemptions:
        #         try:
        #             ros_gateway.cancel_current_work(
        #                 robot_id, lambda oid=order_id, rid=robot_id: self._start_after_return_stop(oid, rid)
        #             )
        #         except (ValueError, RuntimeError) as exc:
        #             self.fail(order_id, str(exc))
        #     for row in active:
        #         await self._tick_real(row)
        #     await self._tick_real_returns()
        return kev_request, active, scheduled_order_id

    # 복귀 주행 정지 후 다음 주문을 시작한다.
    def _start_after_return_stop(self, order_id, robot_id):
        with self.lock:
            if not self.is_current(order_id, robot_id, "WAITING_RETURN_STOP"):
                return
            robot = fleet_manager.get_robot(robot_id)
            if robot is None or not robot.connected or robot.occupied_node is None:
                self.fail(order_id, "복귀 정지 후 로봇 위치를 확인할 수 없습니다.")
                return
            row = self.orders[order_id]
            robot.assign_order(order_id, row["items"], row["total_quantity"], row["pickup_nodes"], None)
            row["phase"] = "MOVING_TO_PICKUP"

    # 실제 로봇 주문의 현재 단계를 진행한다.
    async def _tick_real(self, row):
        order_id = row["order_id"]
        robot = fleet_manager.get_robot(row["robot_id"])
        if robot is None or not robot.connected or robot.state in (RobotState.PAUSED, RobotState.EMERGENCY_STOP):
            return
        phase = row["phase"]
        if phase == "WAITING_WORKSTATION":
            node = workstation_manager.choose(order_id, "real", row["operation_mode"])
            if node is None:
                return
            with self.lock:
                if not self.is_current(order_id, robot.robot_id, phase):
                    workstation_manager.release(order_id)
                    return
                row["workstation_node"] = robot.order_workstation_node = node
                row["phase"] = "MOVING_TO_WORKSTATION"
                row["route_started"] = False
            phase = "MOVING_TO_WORKSTATION"
        if phase not in ("MOVING_TO_PICKUP", "MOVING_TO_WORKSTATION"):
            return
        target = row["pickup_nodes"][row["pickup_index"]] if phase == "MOVING_TO_PICKUP" else row["workstation_node"]
        if robot.current_node == target and robot.state == RobotState.WAITING:
            self.set_phase(
                order_id,
                "WORKING_AT_PICKUP" if phase == "MOVING_TO_PICKUP" else "WORKING_AT_WORKSTATION",
                expected=phase,
            )
            return
        with self.lock:
            if row["phase"] == phase and not row["route_started"] and order_id not in self._route_tasks:
                row["route_started"] = True
                self._route_tasks[order_id] = asyncio.create_task(self._move_real(row, target, phase))

    # 실제 로봇을 다음 주문 목적지로 이동시킨다.
    async def _move_real(self, row, target, phase):
        order_id = row["order_id"]
        try:
            robot = fleet_manager.get_robot(row["robot_id"])
            start = robot.occupied_node if robot else None
            if not self.is_current(order_id, row["robot_id"], phase):
                return
            if start is None:
                raise ValueError("실제 로봇의 출발 노드를 확인할 수 없습니다.")
            await dispatch_order_route(
                row, target, start, valid=lambda: self.is_current(order_id, row["robot_id"], phase)
            )
        except Exception as exc:
            if self.is_current(order_id, row["robot_id"], phase):
                self.fail(order_id, str(exc))
        finally:
            self._route_tasks.pop(order_id, None)

    # 실제 로봇의 복귀 상태를 갱신한다.
    async def _tick_real_returns(self):
        for robot_id, order_id in list(self.recovering.items()):
            row = self.orders.get(order_id)
            robot = fleet_manager.get_robot(robot_id)
            if row and row["status"] == "CANCELLED" and row.get("recovery_status") == "BLOCKED" and robot and robot.connected:
                if robot.occupied_node == CHARGING_STATION_NODES.get(robot_id) and robot.state == RobotState.IDLE and not ros_gateway.navigation_active(robot_id):
                    self.recovering.pop(robot_id, None)
                    row["recovery_status"] = "RETURNED"
                    robot.returning = False
        for robot_id in list(self.returning):
            robot = fleet_manager.get_robot(robot_id)
            station = CHARGING_STATION_NODES.get(robot_id)
            if robot is None or not robot.connected or station is None:
                continue
            order_id = self.returning[robot_id]
            row = self.orders[order_id]
            if (
                robot.occupied_node == station
                and robot.state == RobotState.IDLE
                and not ros_gateway.navigation_active(robot_id)
            ):
                self.returning.pop(robot_id, None)
                if row["status"] == "CANCELLED":
                    self.recovering.pop(robot_id, None)
                    row["recovery_status"] = "RETURNED"
                else:
                    row["phase"] = "COMPLETED"
                self._return_started.discard(robot_id)
                robot.returning = False
                continue
            key = f"return:{robot_id}"
            if robot_id not in self._return_started and key not in self._route_tasks:
                self._return_started.add(robot_id)
                self._route_tasks[key] = asyncio.create_task(self._return_real(robot_id, station, key))

    # 실제 로봇의 충전소 복귀를 요청한다.
    async def _return_real(self, robot_id, station, key):
        order_id = self.returning.get(robot_id)
        if order_id is None:
            self._route_tasks.pop(key, None)
            return
        try:
            row = self.orders[order_id]
            robot = fleet_manager.get_robot(robot_id)
            if robot is None or robot.occupied_node is None:
                raise ValueError("복귀 출발 노드를 확인할 수 없습니다.")
            route_order = row
            if row["status"] == "CANCELLED":
                route_order = dict(row, operation_mode="algorithm", driving_mode="standard")
            await dispatch_order_route(
                route_order, station, robot.occupied_node, valid=lambda: self.returning.get(robot_id) == order_id
            )
        except Exception as exc:
            self.returning.pop(robot_id, None)
            self._return_started.discard(robot_id)
            if self.orders[order_id]["status"] == "CANCELLED":
                self.orders[order_id]["recovery_status"] = "BLOCKED"
                self.orders[order_id]["recovery_error"] = str(exc)
            else:
                self.orders[order_id]["phase"] = "COMPLETED"
                self.orders[order_id]["return_error"] = str(exc)
        finally:
            self._route_tasks.pop(key, None)

    # 실패한 주문을 기록하고 로봇 복구를 시작한다.
    def fail(self, order_id, reason):
        with self.lock:
            row = self.orders.get(order_id)
            if row is None or row["status"] != "IN_PROGRESS":
                return
            failed_phase = row["phase"]
            row["failed_phase"] = failed_phase
            row["failed_at"] = datetime.now(timezone.utc).isoformat()
            row.setdefault("failures", []).append(
                {"phase": failed_phase, "reason": reason, "robot_id": row["robot_id"], "time": row["failed_at"]}
            )
            row["status"] = row["phase"] = "FAILED"
            row["error"] = reason
            workstation_manager.release(order_id)
            if row["mode"] == "simulation":
                from .mock_data import mock_fms

                snapshot = mock_fms.robot_snapshot(row["robot_id"], "simulation")
                row["failure_robot_state"] = {
                    key: snapshot.get(key)
                    for key in ("status", "occupied_node", "occupied_edge", "goal_node", "stop_requested")
                }
                can_recover = (
                    snapshot["connection_state"] == "ONLINE"
                    and snapshot["status"] != "MOVING"
                    and (not snapshot.get("stop_requested") or row.get("_held_node"))
                    and (snapshot["occupied_node"] is not None or snapshot["occupied_edge"] is not None)
                )
                mock_fms.stop_robot(row["robot_id"])
                row["recovery_status"] = "PENDING" if can_recover else "BLOCKED"
                if can_recover:
                    self.recovering[row["robot_id"]] = order_id
            else:
                robot = fleet_manager.get_robot(row["robot_id"])
                if robot:
                    robot.pause("next_destination_failed", reason)
            print(
                f"[{row['failed_at']}] [ORDER FAILED] name={row['order_name']} id={order_id} "
                f"robot={row['robot_id']} phase={failed_phase} reason={reason}",
                flush=True,
            )

    # MQTT 작업 결과를 주문 단계에 반영한다.
    def on_mqtt_result(self, omx, data):
        # MQTT 콜백은 단계만 변경한다. 다음 경로는 FastAPI 루프에서 시작한다.
        with ros_gateway._navigation.lock, self.lock:
            order_id = data.get("job_id")
            row = self.orders.get(order_id)
            if not row or row["mode"] != "real" or row["status"] != "IN_PROGRESS":
                return
            robot = fleet_manager.get_robot(row["robot_id"])
            if (
                robot is None
                or robot.order_id != order_id
                or robot.state != RobotState.WAITING
                or robot.current_node is None
                or NODE_OMX_MAP.get(robot.current_node) != omx.omx_id
            ):
                return
            if row["phase"] in ("WORKING_AT_PICKUP", "MOVING_TO_PICKUP"):
                if robot.current_node != row["pickup_nodes"][row["pickup_index"]]:
                    return
            elif row["phase"] in ("WORKING_AT_WORKSTATION", "MOVING_TO_WORKSTATION"):
                if robot.current_node != row["workstation_node"]:
                    return
            else:
                return
            if not data.get("success", False):
                self.fail(order_id, f"OMX 작업 실패: {omx.omx_id}")
                return
            if row["phase"] in ("WORKING_AT_PICKUP", "MOVING_TO_PICKUP"):
                row["pickup_index"] += 1
                robot.order_pickup_index = row["pickup_index"]
                if row["pickup_index"] < len(row["pickup_nodes"]):
                    row["phase"] = "MOVING_TO_PICKUP"
                    row["route_started"] = False
                else:
                    row["phase"] = "WAITING_WORKSTATION"
            else:
                row["status"] = "COMPLETED"
                row["phase"] = "RETURNING"
                workstation_manager.release(order_id)
                robot.clear_order()
                robot.returning = True
                self.returning[robot.robot_id] = order_id


order_manager = OrderManager()
mqtt_manager.set_result_callback(order_manager.on_mqtt_result)
