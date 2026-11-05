"""WAF 전용 작업 상태. 기존 OMX/주문/AMR 상태를 변경하지 않는다."""
from copy import deepcopy
import json
import logging
import os
from pathlib import Path
from threading import RLock
from time import time
from uuid import uuid4
from .waf_telemetry import waf_telemetry

LOG = logging.getLogger(__name__)
DESTINATIONS = {"A": 1, "B": 2, "C": 3, "D": 4}
ACTIVE = {"SENDING", "RUNNING", "CANCELLING", "UNKNOWN"}


class ReplenishmentError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


class PalletReplenishment:
    def __init__(self, state_path=None, clock=time, telemetry=None):
        self.lock = RLock()
        self.telemetry = telemetry
        self.client = None
        self.goal_factory = None
        self.goal_handle = None
        self.pending_goal = False
        self.cancel_future = None
        self.cancel_started_at = None
        self.clock = clock
        self.path = Path(state_path) if state_path else None
        self.job = None
        self.interface_error = "WAF 액션 인터페이스 준비 중"
        self.closed = False
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                if not isinstance(data, dict) or data.get("state") not in ACTIVE | {"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED", "ENDED"} or not isinstance(data.get("request_id"), str):
                    raise ValueError("invalid WAF journal")
                if data.get("state") in ACTIVE:
                    data.update(state="UNKNOWN", message="FMS 재시작 전 보충 작업의 종료를 확인해야 합니다. 자동 재전송하지 않습니다.")
                    if data.get("ros_goal_id"):
                        data["cancel_requested"] = False  # 새 프로세스에서 동일 UUID 취소를 다시 요청할 수 있다.
                self.job = data
            except (OSError, ValueError, AttributeError):
                self.job = dict(state="UNKNOWN", item=None, request_id=None,
                                message="이전 WAF 작업 기록을 읽지 못했습니다. 현장 확인이 필요합니다.")

    def attach(self, client, goal_factory):
        with self.lock:
            self.client, self.goal_factory = client, goal_factory
            self.interface_error = ""
            self.closed = False

    def unavailable(self, reason):
        with self.lock:
            self.interface_error = reason

    def _save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            try:
                with temporary.open("w") as file:
                    json.dump(self.job, file, ensure_ascii=False)
                    file.flush()
                    os.fsync(file.fileno())
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def _ready(self):
        try:
            return not self.closed and self.client is not None and self.client.server_is_ready()
        except Exception:
            return False

    def snapshot(self):
        with self.lock:
            self._sync_terminal_status()
            ready = self._ready()
            robot = self.telemetry.snapshot() if self.telemetry is not None else None
            own_active = bool(self.job and self.job["state"] in ACTIVE)
            if robot is not None:
                if self.job and self.job["state"] in ACTIVE:
                    robot["activity"] = self.job["state"]
                elif robot["external_active"]:
                    robot["activity"] = "RUNNING"
                else:
                    robot["activity"] = "READY" if ready else "UNKNOWN"
                robot["pixel_x"] = robot["pixel_y"] = None
                if robot["pose"] is not None:
                    from .map_service import world_to_pixel
                    try:
                        robot["pixel_x"], robot["pixel_y"] = world_to_pixel(robot["pose"]["x"], robot["pose"]["y"])
                    except (OSError, ValueError, KeyError):
                        # 지도 파일 문제로 배터리·피드백 수신 화면까지 막지 않는다.
                        pass
            return dict(
                robot=robot,
                action_name=getattr(self.client, "action_name", "/pallet_replace"),
                available=ready,
                can_cancel=own_active and not self.job.get("cancel_requested", False) and bool(
                    self.goal_handle is not None or self.pending_goal or self.job.get("ros_goal_id")),
                can_clear=bool(ready and self.job and self.job["state"] == "UNKNOWN"
                               and self.goal_handle is None and not self.pending_goal
                               and not (robot and robot["external_active"])),
                busy=bool(own_active or (robot and robot["external_active"])),
                reason=self.interface_error or ("" if ready else "WAF /pallet_replace 서버 연결 대기"),
                destinations=dict(DESTINATIONS), job=deepcopy(self.job),
            )

    def _update(self, request_id, **changes):
        if not self.job or self.job.get("request_id") != request_id:
            return False
        self.job.update(changes, updated_at=self.clock())
        try:
            self._save()
        except OSError:
            LOG.exception("WAF 작업 기록 저장 실패")
            self.job.update(state="UNKNOWN", message="작업 기록 저장 실패. WAF 실제 상태를 확인하세요.")
        return True

    def _matches(self, request_id):
        return bool(self.job and self.job.get("request_id") == request_id)

    def _terminal(self, request_id, state, message, status=None):
        if not self._matches(request_id):
            return
        if status in (0, 4, 5, 6) and self.telemetry is not None and self.job.get("ros_goal_id"):
            self.telemetry.complete_goal(self.job["ros_goal_id"], status)
        self.goal_handle = None
        self.pending_goal = False
        self.cancel_future = None
        self.cancel_started_at = None
        self._update(request_id, state=state, message=message, cancel_requested=False)

    def _sync_terminal_status(self):
        if not self.job or self.job["state"] not in ACTIVE or self.telemetry is None:
            return
        goal_id = self.job.get("ros_goal_id")
        status = self.telemetry.goal_status(goal_id) if goal_id else None
        if status == 5:
            self._terminal(self.job["request_id"], "CANCELLED", "WAF 취소 완료 확인 · 새 보충 요청 가능", status)
        elif status == 6:
            self._terminal(self.job["request_id"], "FAILED", "WAF 작업 중단 확인 · 새 보충 요청 가능", status)
        elif status == 4:
            # 종료 상태만으로 PalletReplace.Result.success나 물품 교체 성공을 만들지 않는다.
            self._terminal(self.job["request_id"], "ENDED", "WAF 작업 종료 확인 · 상세 결과 미수신", status)

    def start(self, item, request_id):
        with self.lock:
            self._sync_terminal_status()
            if self.job and self.job.get("request_id") == request_id:
                if self.job.get("item") != item:
                    raise ReplenishmentError(409, "같은 요청 ID로 품목을 바꿀 수 없습니다")
                return self.snapshot()
            if self.job and self.job["state"] in ACTIVE:
                raise ReplenishmentError(409, "WAF 보충 작업이 진행 중이거나 종료 확인이 필요합니다")
            if self.telemetry is not None and self.telemetry.external_active():
                raise ReplenishmentError(409, "WAF에서 이미 액션을 수행 중입니다. 종료 결과를 기다리세요")
            if item not in DESTINATIONS:
                raise ReplenishmentError(400, "지원하지 않는 품목입니다")
            if not self._ready():
                raise ReplenishmentError(503, self.snapshot()["reason"])
            self.job = dict(request_id=request_id, item=item, destination=DESTINATIONS[item],
                            ros_goal_id=uuid4().hex,
                            action_name=getattr(self.client, "action_name", "/pallet_replace"),
                            state="SENDING", stage="", message="WAF 접수 확인 중", cancel_requested=False,
                            started_at=self.clock(), updated_at=self.clock())
            self.goal_handle = None
            self.cancel_future = None
            self.cancel_started_at = None
            try:
                self._save()  # 전송 전에 기록한다. 저장할 수 없으면 물리 명령을 보내지 않는다.
            except OSError as exc:
                self.job = None
                raise ReplenishmentError(503, "WAF 작업 기록 저장 실패. 명령을 보내지 않았습니다") from exc
            try:
                goal = self.goal_factory()
                goal.destination = DESTINATIONS[item]
                self.pending_goal = True
                future = self.client.send_goal_async(goal, goal_id=self.job["ros_goal_id"],
                    feedback_callback=lambda msg: self.feedback(request_id, msg))
                future.add_done_callback(lambda response: self.accepted(request_id, response))
            except Exception:
                self.pending_goal = False
                LOG.exception("WAF 전송 결과 확인 실패")
                self._update(request_id, state="UNKNOWN", message="명령 전송 여부를 확인하지 못했습니다. 자동 재전송하지 않습니다.")
            return self.snapshot()

    def feedback(self, request_id, msg):
        with self.lock:
            if not self.job or self.job["state"] not in ACTIVE:
                return
            stage = str(msg.feedback.stage)[:100]
            self._update(request_id, stage=stage, message="취소 처리 중 · 실제 종료 결과 대기" if self.job.get("cancel_requested") else "보충 작업 진행 중")

    def accepted(self, request_id, future):
        with self.lock:
            if not self._matches(request_id):
                return
            try:
                handle = future.result()
                self.pending_goal = False
                if not handle.accepted:
                    self._terminal(request_id, "REJECTED", "WAF 서버가 작업을 거절했습니다. 다른 작업 진행 여부를 확인하세요.")
                    return
                if self.job["state"] not in ACTIVE:
                    return  # 지연된 접수 응답이 이미 종료된 작업을 다시 RUNNING으로 만들지 않는다.
                self.goal_handle = handle
                self._update(request_id, state="RUNNING", message="WAF 작업 접수 완료")
                handle.get_result_async().add_done_callback(lambda result: self.finished(request_id, result))
                if self.job.get("cancel_requested"):
                    self._cancel(request_id)
            except Exception:
                LOG.exception("WAF 접수 확인 실패")
                self._update(request_id, state="UNKNOWN", message="WAF 접수·결과 연결을 확인하지 못했습니다. 실제 상태 확인 필요")

    def finished(self, request_id, future):
        with self.lock:
            if not self._matches(request_id):
                return
            try:
                response = future.result()
                # ROS GoalStatus: SUCCEEDED=4, CANCELED=5, ABORTED=6.
                if response.status == 5:
                    state = "CANCELLED"
                elif response.status == 4 and response.result.success:
                    state = "SUCCEEDED"
                elif response.status in (4, 6):
                    state = "FAILED"
                else:
                    state = "UNKNOWN"
                if state == "UNKNOWN":
                    if self.job["state"] in ACTIVE:
                        self._update(request_id, state=state, message=str(response.result.message)[:1000])
                else:
                    self._terminal(request_id, state, str(response.result.message)[:1000], response.status)
            except Exception:
                LOG.exception("WAF 결과 확인 실패")
                if self.job["state"] in ACTIVE:
                    self._update(request_id, state="UNKNOWN", message="WAF 종료 결과 수신 실패. 종료 상태를 다시 확인합니다.")
                    self._sync_terminal_status()

    def cancel(self, request_id):
        with self.lock:
            self._sync_terminal_status()
            if not self.job or self.job.get("request_id") != request_id:
                raise ReplenishmentError(409, "현재 보충 작업 ID와 다릅니다")
            if self.job["state"] not in ACTIVE:
                return self.snapshot()
            if self.goal_handle is None and not self.pending_goal and not self.job.get("ros_goal_id"):
                raise ReplenishmentError(409, "이전 실행 작업은 FMS에서 취소할 수 없습니다. WAF에서 실제 종료를 확인하세요")
            if self.job.get("cancel_requested"):
                return self.snapshot()
            self._update(request_id, cancel_requested=True, message="취소 요청 중 · WAF 종료 결과 대기")
            self.cancel_started_at = self.clock()
            if self.goal_handle is not None or (self.job.get("ros_goal_id") and
                    (not self.pending_goal or self.clock() - self.job["started_at"] > 15)):
                self._cancel(request_id)
            return self.snapshot()

    def _cancel(self, request_id):
        try:
            self._update(request_id, state="CANCELLING")
            future = (self.goal_handle.cancel_goal_async() if self.goal_handle is not None else
                      self.client.cancel_goal_async(self.job["ros_goal_id"], self.job["action_name"]))
            self.cancel_future = future
            self.cancel_started_at = self.clock()
            def cancelled(reply):
                with self.lock:
                    if not self._matches(request_id) or self.cancel_future is not reply or self.job["state"] not in ACTIVE:
                        return
                    self.cancel_future = None
                    try:
                        response = reply.result()
                        if getattr(response, "return_code", None) == 3:  # CancelGoal.ERROR_GOAL_TERMINATED
                            self._terminal(request_id, "ENDED",
                                           "WAF 서버가 이전 작업 종료 확인 · 상세 결과 미수신 · 새 보충 요청 가능", 0)
                        elif not response.goals_canceling:
                            self.cancel_started_at = None
                            self._update(request_id, state="RUNNING", cancel_requested=False,
                                         message="취소가 접수되지 않았습니다. 종료 상태 확인 또는 취소 재시도 가능")
                    except Exception:
                        self.cancel_started_at = None
                        self._update(request_id, state="UNKNOWN", cancel_requested=False,
                                     message="취소 응답 수신 실패 · 취소 재시도 가능. 새 작업은 종료 확인 후 허용")
                    self._sync_terminal_status()
            future.add_done_callback(cancelled)
        except Exception:
            self.cancel_future = None
            self.cancel_started_at = None
            self._update(request_id, state="UNKNOWN", cancel_requested=False,
                         message="취소 전송 결과 미확인 · 연결 확인 후 취소 재시도 가능")

    def clear_stopped(self, request_id):
        """핸들이 사라진 이전 기록만 운영자의 실제 종료 확인을 받아 정리한다."""
        with self.lock:
            if not self._matches(request_id):
                raise ReplenishmentError(409, "현재 보충 작업 ID와 다릅니다")
            row = self.snapshot()
            if not row["can_clear"]:
                raise ReplenishmentError(409, "현재 작업은 정리할 수 없습니다. 진행 중이면 먼저 취소하세요")
            self._terminal(request_id, "CANCELLED" if self.job.get("cancel_requested") else "FAILED",
                           "운영자가 이전 WAF 작업 종료 확인 · 기록 정리 완료 · 새 보충 요청 가능")
            return self.snapshot()

    def tick(self):
        with self.lock:
            self._sync_terminal_status()
            if self.cancel_started_at is not None and self.clock() - self.cancel_started_at > 15:
                expired = self.cancel_future
                self.cancel_future = None  # 늦은 응답이 이후 요청 상태를 바꾸지 않도록 분리한다.
                self.cancel_started_at = None
                if expired is not None:
                    expired.cancel()  # rclpy의 미완료 취소 요청도 해제한다. 실제 goal 취소가 아니다.
                if self.job and self.job["state"] in ACTIVE:
                    self._update(self.job["request_id"], state="UNKNOWN", cancel_requested=False,
                                 message="취소 완료 응답 지연 · 취소 재시도 가능. 실제 종료 확인 전 새 작업은 차단")
            if self.job and self.job["state"] == "SENDING" and self.clock() - self.job["started_at"] > 15:
                self._update(self.job["request_id"], state="UNKNOWN", message="WAF 접수 응답 지연. 중복 전송 없이 응답을 기다립니다.")

    def close(self):
        with self.lock:
            self._sync_terminal_status()
            if self.job and self.job["state"] in ACTIVE:
                self._update(self.job.get("request_id"), state="UNKNOWN", message="FMS 종료. WAF 작업의 실제 종료 확인 필요")
            self.closed = True
            self.client = None


replenishment = PalletReplenishment(
    Path(os.environ.get("FMS_LOG_DIR", Path(__file__).resolve().parents[3] / "logs")) / "waf_replenishment.json", telemetry=waf_telemetry
)
