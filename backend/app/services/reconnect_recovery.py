"""장시간 단절 복구. 기존 루프/잠금을 사용하며 별도 스레드나 자동 재주문은 없다."""

from time import monotonic

from ..models.robot import RobotState


class ReconnectRecovery:
    def __init__(self, gateway):
        self.gateway = gateway
        self.pending = {}

    def begin(self, robot, disconnected_s):
        # 호출자는 Gateway의 navigation lock을 보유한다.
        from ..ros2.ros_gateway import mqtt_manager
        from .order_manager import order_manager

        rid = robot.robot_id
        old = self.pending.get(rid, {})
        jobs = set(old.get("jobs", ()))
        if robot.order_id:
            jobs.add(robot.order_id)
        waiting = self.gateway._waiting_omx.pop(rid, None)
        if waiting:
            jobs.add(waiting[0])
        robot.reconnect_reset_pending = True
        if robot.state != RobotState.EMERGENCY_STOP:
            robot.pause("long_disconnect_reset")
        discarded, tasks = order_manager.discard_disconnected_robot_work(rid)
        jobs.update(discarded)
        tasks = list(dict.fromkeys([*old.get("tasks", ()), *tasks]))
        robot.clear_order()
        robot.returning = False
        self.gateway._dock_attempts.pop(rid, None)
        record = dict(jobs=jobs, tasks=tasks, reconnected_at=monotonic(), stopped=False, retry_at=0.0)
        self.pending[rid] = record
        for job in jobs:
            # cancel_job는 물리 OMX 취소가 아니다. 늦은 Result를 무효화하고 종료를 기다린다.
            mqtt_manager.cancel_job(job)
        print(f"[ROBOT RECONNECT] {rid}: {disconnected_s:.1f}초 단절 후 이전 작업 초기화. "
              "정지·새 위치·OMX 종료 확인 중; 이전 주문 재실행 없음", flush=True)
        self._request_stop(rid, record)

    def _request_stop(self, rid, record):
        record["retry_at"] = monotonic() + 5.0

        def stopped():
            if self.pending.get(rid) is record:
                record["stopped"] = True

        self.gateway.navigation.stop(rid, stopped, reason="long_disconnect_reset")

    def tick(self):
        from ..ros2.ros_gateway import fleet_manager, mqtt_manager

        with self.gateway.navigation.lock:
            for rid, record in list(self.pending.items()):
                robot = fleet_manager.get_robot(rid)
                if robot is None or not robot.connected:
                    continue
                if robot.state != RobotState.EMERGENCY_STOP and robot.pause_reason_code != "long_disconnect_reset":
                    robot.pause("long_disconnect_reset")
                if not record["stopped"]:
                    if monotonic() >= record["retry_at"]:
                        self._request_stop(rid, record)
                    continue
                if self.gateway._ros_node.has_active_auxiliary(rid):
                    continue
                if any(mqtt_manager.has_active_job(job) for job in record["jobs"]):
                    continue
                if any(not task.done() for task in record["tasks"]):
                    continue  # 이전 Task의 finally가 새 주문 문맥을 지우지 않도록 종료를 확인한다.
                try:
                    ready = self.gateway.navigation.reset_reconnected_robot(rid, record["reconnected_at"])
                except (OSError, KeyError, ValueError, RuntimeError):
                    continue  # 점유/지도 미확정 시 예약을 유지하고 다음 기존 루프에서 재확인.
                if not ready:
                    continue
                for context in (self.gateway._charging_docks, self.gateway._departing_docks,
                                self.gateway._consumed_contacts):
                    context.pop(rid, None)
                self.gateway._unverified_docks.discard(rid)
                self.gateway._station_exit_manual_ready.discard(rid)
                robot.docked_station = None
                robot.reconnect_reset_pending = False
                if robot.state != RobotState.EMERGENCY_STOP:
                    robot.set_state(RobotState.IDLE)
                self.pending.pop(rid, None)
                print(f"[ROBOT RECONNECT] {rid}: 이전 작업 정리·정지·새 위치 확인 완료. "
                      f"현재 상태={robot.state.value}; 새 주문만 접수", flush=True)
