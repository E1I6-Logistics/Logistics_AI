"""공통 주문 대기열, 배차, Real 작업 단계."""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from threading import RLock
from time import monotonic
from uuid import uuid4

from ..models.robot import RobotState
from ..ros2.ros_gateway import CHARGING_STATION_NODES, NODE_OMX_MAP, ros_gateway
from ..schemas.robot import normalize_robot_id
from .fleet_manager import fleet_manager
from .mode_service import mode_manager, feature_engine
from .mqtt_manager import mqtt_manager
from .order_dispatcher import available_robots
from .order_route_dispatcher import dispatch_order_route
from .scenario_service import scenario_manager
from .workstation_manager import workstation_manager


logger = logging.getLogger(__name__)

# 배차 KEV 호출은 주문 tick 안에서 동기로 실행되므로 짧게 끊고, 실패 후에는 잠시 호출하지 않는다.
KEV_DISPATCH_TIMEOUT_SECONDS = float(os.getenv("KEV_DISPATCH_TIMEOUT_SECONDS", "3"))
KEV_DISPATCH_COOLDOWN_SECONDS = float(os.getenv("KEV_DISPATCH_COOLDOWN_SECONDS", "30"))


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

    # 배차 가능한 후보 중 한 대를 KEV로 선택한다.
    def _select_robot_with_kev(self, row, candidates, mode):
        if not candidates:
            return None
        # 재시도 주문은 다른 후보가 있으면 직전 로봇을 KEV 후보에서 뺀다.
        # V1 지시문의 "Avoid the last robot"을 지시문 대신 코드 규칙으로 옮긴 것이다.
        last_robot_id = row.get("last_robot_id")
        if last_robot_id in candidates and len(candidates) > 1:
            candidates = [candidate for candidate in candidates if candidate != last_robot_id]
        # Kev choice API는 후보가 두 개 이상이어야 한다.
        if len(candidates) == 1:
            logger.info("KEV dispatch skipped (single candidate): robot=%s order=%s", candidates[0], row["order_id"])
            return candidates[0]

        profiles = []
        for robot_id in candidates:
            profile = self._dispatch_robot_profile(robot_id, mode)
            if profile is not None:
                profiles.append(profile)
        if not profiles:
            return None
        profiles.sort(key=lambda profile: profile["robot_id"])
        if len(profiles) == 1:
            logger.info(
                "KEV dispatch skipped (single profile): robot=%s order=%s", profiles[0]["robot_id"], row["order_id"]
            )
            return profiles[0]["robot_id"]

        if self._kev_dispatch_selector is None:
            from .route_selector.kev_selector import KevSelector

            # 기존 코드. 기본 KEV_TIMEOUT_SECONDS(60초)를 그대로 써서 tick이 오래 멈출 수 있었다.
            # self._kev_dispatch_selector = KevSelector()
            # KEV_MODEL, KEV_HOST 등은 환경변수에서 읽는다. 배차 timeout만 경로 선택과 따로 둔다.
            self._kev_dispatch_selector = KevSelector(timeout_seconds=KEV_DISPATCH_TIMEOUT_SECONDS)

        choices = {
            profile["robot_id"]: (
                f"state={profile['state']}, "
                f"battery={profile['battery']}, "
                f"current_node={profile['current_node']}, "
                f"occupied_node={profile['occupied_node']}, "
                f"goal_node={profile['goal_node']}, "
                f"returning={profile['returning']}"
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
        }
        # 기존 지시문(V1). D-3에서 최고 배터리 선택 정확도가 2/6이어서 V3로 바꾼다.
        # instructions = (
        #     "Choose exactly one available robot for this order. "
        #     "Prefer an idle and non-returning robot. "
        #     "Prefer higher battery when other conditions are similar. "
        #     "Avoid the last robot when another suitable robot is available. "
        #     "Return only one candidate ID."
        # )
        instructions = (
            "Among idle and non-returning robots, choose the one with the highest battery. "
            "Use other fields only to break exact ties. Return only one candidate ID."
        )
        question_id = f"dispatch_{row['order_id']}"
        debug_enabled = os.getenv("KEV_DISPATCH_DEBUG") == "1"
        debug_record = {
            "order_id": row["order_id"],
            "question_id": question_id,
            "state": state,
            "choices": choices,
            "instructions": instructions,
        }
        try:
            result = self._kev_dispatch_selector.select_choice(
                state=state,
                candidates=choices,
                instructions=instructions,
                question_id=question_id,
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
        logger.info(
            "KEV dispatch selected robot=%s order=%s candidates=%s", chosen_robot, row["order_id"], candidates
        )
        return chosen_robot

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
            for row in self.orders.values():
                if row["mode"] != mode or row["status"] != "PENDING" or not candidates:
                    continue
                blocked_by_earlier_pickup = False
                if mode == "simulation":
                    for previous in self.orders.values():
                        if previous is row:
                            break
                        if (
                            previous["mode"] == mode
                            and previous["status"] == "IN_PROGRESS"
                            and set(previous["pickup_nodes"]).intersection(row["pickup_nodes"])
                        ):
                            blocked_by_earlier_pickup = True
                            break

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
                if dispatch_engine == "ai" and monotonic() >= self._kev_dispatch_retry_at:
                    try:
                        robot_id = self._select_robot_with_kev(row, eligible_candidates, mode)
                        if robot_id is not None:
                            candidates.remove(robot_id)
                    except Exception as exc:
                        # KEV 서버 오류나 잘못된 응답은 주문 실패가 아니라 기존 배차로 복구한다.
                        # 실패가 반복되면 tick마다 timeout을 기다리지 않도록 잠시 KEV 호출을 멈춘다.
                        self._kev_dispatch_retry_at = monotonic() + KEV_DISPATCH_COOLDOWN_SECONDS
                        logger.warning(
                            "KEV dispatch failed; falling back to algorithm for %.0fs: order=%s error=%r",
                            KEV_DISPATCH_COOLDOWN_SECONDS,
                            row["order_id"],
                            exc,
                            exc_info=True,
                        )
                        robot_id = None
                elif dispatch_engine == "ai":
                    logger.info(
                        "KEV dispatch skipped (cooldown %.1fs left): order=%s",
                        self._kev_dispatch_retry_at - monotonic(),
                        row["order_id"],
                    )

                if robot_id is None:
                    robot_id = self._select_robot_by_algorithm(row, candidates, blocked_by_earlier_pickup)
                if robot_id is None:
                    continue
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
