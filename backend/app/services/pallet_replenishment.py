"""WAF 전용 작업 상태. 기존 OMX/주문/AMR 상태를 변경하지 않는다."""
from copy import deepcopy
import json
import logging
import os
from pathlib import Path
from threading import RLock
from time import time
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
        self.clock = clock
        self.path = Path(state_path) if state_path else None
        self.job = None
        self.interface_error = "WAF 액션 인터페이스 준비 중"
        self.closed = False
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                if not isinstance(data, dict) or data.get("state") not in ACTIVE | {"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"} or not isinstance(data.get("request_id"), str):
                    raise ValueError("invalid WAF journal")
                if data.get("state") in ACTIVE:
                    data.update(state="UNKNOWN", message="FMS 재시작 전 보충 작업의 종료를 확인해야 합니다. 자동 재전송하지 않습니다.")
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
                can_cancel=own_active and bool(self.goal_handle is not None or self.pending_goal),
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

    def start(self, item, request_id):
        with self.lock:
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
                            action_name=getattr(self.client, "action_name", "/pallet_replace"),
                            state="SENDING", stage="", message="WAF 접수 확인 중", cancel_requested=False,
                            started_at=self.clock(), updated_at=self.clock())
            self.goal_handle = None
            try:
                self._save()  # 전송 전에 기록한다. 저장할 수 없으면 물리 명령을 보내지 않는다.
            except OSError as exc:
                self.job = None
                raise ReplenishmentError(503, "WAF 작업 기록 저장 실패. 명령을 보내지 않았습니다") from exc
            try:
                goal = self.goal_factory()
                goal.destination = DESTINATIONS[item]
                self.pending_goal = True
                future = self.client.send_goal_async(goal, feedback_callback=lambda msg: self.feedback(request_id, msg))
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
            self.pending_goal = False
            try:
                handle = future.result()
                if not self.job or self.job.get("request_id") != request_id:
                    return
                if not handle.accepted:
                    self._update(request_id, state="REJECTED", message="WAF 서버가 작업을 거절했습니다. 다른 작업 진행 여부를 확인하세요.")
                    return
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
                self._update(request_id, state=state, message=str(response.result.message)[:1000])
            except Exception:
                LOG.exception("WAF 결과 확인 실패")
                self._update(request_id, state="UNKNOWN", message="WAF 종료 결과 수신 실패. 자동 재실행하지 않습니다.")

    def cancel(self, request_id):
        with self.lock:
            if not self.job or self.job.get("request_id") != request_id:
                raise ReplenishmentError(409, "현재 보충 작업 ID와 다릅니다")
            if self.job["state"] not in ACTIVE:
                return self.snapshot()
            if self.goal_handle is None and not self.pending_goal:
                raise ReplenishmentError(409, "이전 실행 작업은 FMS에서 취소할 수 없습니다. WAF에서 실제 종료를 확인하세요")
            if self.job.get("cancel_requested"):
                return self.snapshot()
            self._update(request_id, cancel_requested=True, message="취소 요청 중 · WAF 종료 결과 대기")
            if self.goal_handle:
                self._cancel(request_id)
            return self.snapshot()

    def _cancel(self, request_id):
        try:
            self._update(request_id, state="CANCELLING")
            future = self.goal_handle.cancel_goal_async()
            def cancelled(reply):
                with self.lock:
                    try:
                        if not reply.result().goals_canceling:
                            self._update(request_id, message="취소 접수 미확인. 실제 종료 결과를 기다립니다.")
                    except Exception:
                        self._update(request_id, message="취소 응답 수신 실패. 실제 종료 결과를 기다립니다.")
            future.add_done_callback(cancelled)
        except Exception:
            self._update(request_id, state="UNKNOWN", message="취소 전송 결과를 확인하지 못했습니다")

    def tick(self):
        with self.lock:
            if self.job and self.job["state"] == "SENDING" and self.clock() - self.job["started_at"] > 15:
                self._update(self.job["request_id"], state="UNKNOWN", message="WAF 접수 응답 지연. 중복 전송 없이 응답을 기다립니다.")

    def close(self):
        with self.lock:
            if self.job and self.job["state"] in ACTIVE:
                self._update(self.job.get("request_id"), state="UNKNOWN", message="FMS 종료. WAF 작업의 실제 종료 확인 필요")
            self.closed = True
            self.client = None


replenishment = PalletReplenishment(
    Path(os.environ.get("FMS_LOG_DIR", Path(__file__).resolve().parents[3] / "logs")) / "waf_replenishment.json", telemetry=waf_telemetry
)
