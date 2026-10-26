import json
import threading
import time

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

from ..models.omx import OMX
from ..config import MQTT_BROKER_IP, MQTT_BROKER_PORT, DOCK_DEBOUNCE_S, DOCK_SENSOR_MAX_AGE_S


class MQTTManager:
    # MQTTManager의 초기 상태를 설정한다.
    def __init__(self):
        self.broker_ip = MQTT_BROKER_IP
        self.broker_port = MQTT_BROKER_PORT

        # OMX 객체 저장
        self.omx_devices = {}

        # 작업 결과 Callback
        self.result_callback = None
        # (OMX, 주문 ID)별 미완료 명령. 같은 Result로 다음 단계를 두 번 실행하지 않는다.
        self._pending_jobs = set()
        self._pending_lock = threading.Lock()
        self._canceled_jobs = set()
        self._cancel_callbacks = {}

        self._dock_lock = threading.Lock()
        self._dock_samples = {}
        self.dock_status = {}
        self._dock_connected = False
        self._dock_generation = 0

        # MQTT Client
        if mqtt is not None:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
            self.client.on_connect = self._on_connect
            self.client.on_message = self._on_message
            self.client.on_disconnect = self._on_disconnect
        else:
            self.client = None

        self.running = False
        self.connection_thread = None

    # =========================================================
    # MQTT 시작
    # =========================================================

    # 관련을 시작한다.
    def start(self):
        if self.running:
            return

        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [MQTT] "
            f"브로커 연결 시도 address={self.broker_ip}:{self.broker_port}",
            flush=True,
        )
        try:
            self.client.connect(self.broker_ip, self.broker_port, 60)
        except Exception as exc:
            print(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] [MQTT] "
                f"브로커 연결 중 오류 address={self.broker_ip}:{self.broker_port} "
                f"error_type={type(exc).__name__} detail={exc}",
                flush=True,
            )
            raise

        self.client.loop_start()
        self.running = True

        self.connection_thread = threading.Thread(target=self._connection_loop, daemon=True)
        self.connection_thread.start()

    # =========================================================
    # MQTT 종료
    # =========================================================

    # 관련을 정지한다.
    def stop(self):
        self._invalidate_dock_connection(False)
        if not self.running:
            return
        self.running = False

        self.client.loop_stop()
        self.client.disconnect()

        if self.connection_thread is not None:
            self.connection_thread.join(timeout=2.0)

        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [MQTT] Stopped", flush=True)

    # =========================================================
    # Broker 연결 Callback
    # =========================================================

    # 연결 이벤트를 처리한다.
    def _on_connect(self, client, userdata, flags, rc):
        self._invalidate_dock_connection(rc == 0)
        if rc != 0:
            print(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] [MQTT] "
                f"브로커 연결 실패 address={self.broker_ip}:{self.broker_port} code={rc}",
                flush=True,
            )
            return
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [MQTT] "
            f"브로커 연결 완료 address={self.broker_ip}:{self.broker_port}",
            flush=True,
        )

        client.subscribe("+/status")
        client.subscribe("+/ack")
        client.subscribe("+/progress")
        client.subscribe("+/result")
        client.subscribe("dock/station/+/status")

        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [MQTT] Waiting OMX robots...", flush=True)

    # =========================================================
    # 메시지 수신
    # =========================================================

    # message 이벤트를 처리한다.
    def _on_message(self, client, userdata, msg):
        topic_parts = msg.topic.split("/")
        if topic_parts[0] == "dock":
            self._on_dock_message(client, userdata, msg)
            return

        if len(topic_parts) != 2:
            return

        omx_id = topic_parts[0]
        message_type = topic_parts[1]

        try:
            data = json.loads(msg.payload.decode())

        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] [MQTT] "
                f"메시지 해석 실패 omx={omx_id} topic={msg.topic} "
                f"error_type={type(exc).__name__} detail={exc}",
                flush=True,
            )
            return

        # =============================================
        # 처음 보는 OMX
        # =============================================

        if omx_id not in self.omx_devices:

            # status 메시지가 와야 등록
            if message_type != "status":
                return

            self.omx_devices[omx_id] = OMX(omx_id=omx_id, mqtt_client=self.client)

            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [MQTT] New OMX discovered: " f"{omx_id}", flush=True)

        omx = self.omx_devices[omx_id]

        # =============================================
        # 메시지 종류
        # =============================================

        if message_type == "status":
            # status 수신 전 연결 상태 저장
            was_connected = omx.connected
            # status 수신 → 연결 상태 및 last_seen 갱신
            omx.update_status(data)

            # OFFLINE 상태였던 OMX가 다시 status를 보내면 재연결
            if not was_connected:
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [MQTT] OMX RECONNECTED: {omx_id}", flush=True)

        elif message_type == "ack":
            omx.update_ack(data)
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [MQTT] ACK: " f"{omx_id} / {data}", flush=True)
            if data.get("accepted") is False:
                key = (omx_id, data.get("job_id"))
                with self._pending_lock:
                    pending = key in self._pending_jobs
                    self._pending_jobs.discard(key)
                    canceled = key in self._canceled_jobs
                    self._canceled_jobs.discard(key)
                    callback = None
                    if canceled:
                        same_job_still_pending = any(row[1] == key[1] for row in self._pending_jobs)
                        if not same_job_still_pending:
                            callback = self._cancel_callbacks.pop(key[1], None)
                if callback is not None:
                    callback()
                if pending and not canceled and self.result_callback is not None:
                    self.result_callback(omx, {"job_id": key[1], "success": False, "message": "OMX ACK rejected"})

        elif message_type == "progress":
            omx.update_progress(data)
            print(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [MQTT] PROGRESS: "
                f"{omx_id} / "
                f"{omx.current_count}/"
                f"{omx.total_count}",
                flush=True,
            )

        elif message_type == "result":
            omx.update_result(data)
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [MQTT] RESULT: " f"{omx_id} / {data}", flush=True)
            key = (omx_id, data.get("job_id"))
            with self._pending_lock:
                pending = key in self._pending_jobs
                self._pending_jobs.discard(key)
                canceled = key in self._canceled_jobs
                self._canceled_jobs.discard(key)
                callback = None
                if canceled:
                    same_job_still_pending = any(row[1] == key[1] for row in self._pending_jobs)
                    if not same_job_still_pending:
                        callback = self._cancel_callbacks.pop(key[1], None)
            if callback is not None:
                callback()
            if pending and not canceled and self.result_callback is not None:
                self.result_callback(omx, data)
            elif not pending or canceled:
                print(
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [WARN] [MQTT] ignored duplicate/stale Result: "
                    f"omx={omx_id} job_id={key[1]}",
                    flush=True,
                )

    # 센서 상태: MQTT 스레드는 이 저장소만 갱신한다.
    def _invalidate_dock_connection(self, connected):
        with self._dock_lock:
            self._dock_connected = connected
            self._dock_generation += 1
            self._dock_samples.clear()
            self.dock_status.clear()

    def _on_disconnect(self, client, userdata, rc):
        self._invalidate_dock_connection(False)

    def dock_generation(self):
        with self._dock_lock:
            return self._dock_generation

    def get_dock_snapshot(self, station, *, now=None):
        """수신 스레드 밖에서 읽는 사본. 이벤트 방식은 무수신만으로 만료시키지 않는다."""
        now = time.monotonic() if now is None else now
        with self._dock_lock:
            sample = dict(self._dock_samples.get(station, {}))
            connected = self._dock_connected
            generation = self._dock_generation
        received = sample.get("received_at")
        age = None if received is None else max(0.0, now - received)
        trusted = connected and bool(sample) and not sample.get("retained", False)
        if DOCK_SENSOR_MAX_AGE_S > 0 and age is not None and age > DOCK_SENSOR_MAX_AGE_S:
            trusted = False
        contact_since = sample.get("contact_since")
        return {
            **sample,
            "station": station,
            "generation": generation,
            "broker_connected": connected,
            "contact": ("CONTACT" if sample.get("status") == 1 else "RELEASED") if trusted else "UNKNOWN",
            "trusted": trusted,
            "age_s": age,
            "stable": bool(trusted and contact_since is not None and now - contact_since >= DOCK_DEBOUNCE_S),
            "freshness_policy": "expiry" if DOCK_SENSOR_MAX_AGE_S > 0 else "event",
        }

    def dock_contact_valid(self, station, *, started_at, generation, now=None):
        sample = self.get_dock_snapshot(station, now=now)
        return bool(
            sample["generation"] == generation
            and sample["stable"]
            and sample.get("status") == 1
            and sample.get("contact_since", -1) >= started_at
        )

    def _on_dock_message(self, client, userdata, msg):
        try:
            parts = msg.topic.split("/")
            if len(parts) != 4 or parts[:2] != ["dock", "station"] or parts[3] != "status":
                raise ValueError("invalid dock topic")
            if parts[2] not in ("1", "2", "3"):
                raise ValueError("invalid station in topic")
            station = int(parts[2])
            data = json.loads(msg.payload.decode("utf-8"))

            # 잘못된 JSON 형식이나 다른 스테이션의 메시지는 무시
            if not isinstance(data, dict):
                raise ValueError("payload must be an object")
            if type(data.get("station")) is not int or data["station"] != station:
                raise ValueError("station must match topic and be an integer")
            status = data.get("status")
            if type(status) is not int or status not in (0, 1):
                raise ValueError("status must be integer 0 or 1")

            if getattr(msg, "retain", False) or getattr(msg, "dup", False):
                return
            now = time.monotonic()
            with self._dock_lock:
                if not self._dock_connected:
                    return
                previous = self._dock_samples.get(station, {})
                contact_since = None
                if status == 1:
                    contact_since = (
                        previous.get("contact_since")
                        if previous.get("status") == 1
                        else now
                    )
                self.dock_status[station] = status
                self._dock_samples[station] = {
                    "status": status,
                    "state_since": previous.get("state_since", now) if previous.get("status") == status else now,
                    "received_at": now,
                    "received_at_unix": time.time(),
                    "contact_since": contact_since,
                    "retained": False,
                }

            print(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [DOCK] "
                f"station={station} status={status} "
                f"contact={'CONTACT' if status == 1 else 'RELEASED'}",
                flush=True,
            )

        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            print(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [WARN] [DOCK] "
                f"메시지 해석 실패 topic={msg.topic} error={exc}",
                flush=True,
            )


    # OMX 작업 명령
    # =========================================================

    # 작업을 전송한다.
    def send_job(self, omx_id, job_id, items):
        omx = self.get_omx(omx_id)

        if omx is None:
            raise ValueError(f"OMX not found: {omx_id}")

        if not omx.connected:
            raise RuntimeError(f"OMX is offline: {omx_id}")

        key = (omx_id, job_id)
        with self._pending_lock:
            if key in self._pending_jobs:
                raise RuntimeError(f"OMX 작업 명령이 이미 진행 중입니다: {omx_id}/{job_id}")
            self._pending_jobs.add(key)
        try:
            omx.send_command(job_id=job_id, items=items)
        except Exception:
            with self._pending_lock:
                self._pending_jobs.discard(key)
            raise

    def cancel_job(self, job_id, callback=None):
        """기존 Result를 무효화하고 OMX 종료 메시지를 기다린다. 물리 동작 취소는 아니다."""
        with self._pending_lock:
            active = [key for key in self._pending_jobs if key[1] == job_id]
            self._canceled_jobs.update(active)
            if active and callback is not None:
                self._cancel_callbacks[job_id] = callback
        if active:
            print(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [WARN] [MQTT] FMS job invalidated "
                f"job_id={job_id} omx={[key[0] for key in active]} "
                "waiting for OMX terminal message",
                flush=True,
            )
        return not active

    # 활성 작업 여부를 확인한다.
    def has_active_job(self, job_id):
        with self._pending_lock:
            return any(key[1] == job_id for key in self._pending_jobs)

    # =========================================================
    # OMX 조회
    # =========================================================

    # OMX를 조회한다.
    def get_omx(self, omx_id):
        return self.omx_devices.get(omx_id)

    # 모든 OMX 장치 상태를 반환한다.
    def get_all_omx(self):
        return self.omx_devices

    # =========================================================
    # Result Callback
    # =========================================================

    # 작업 결과를 받을 콜백을 등록한다.
    def set_result_callback(self, callback):
        self.result_callback = callback

    # =========================================================
    # 연결 확인
    # =========================================================

    # MQTT 서버 연결과 재연결을 반복 관리한다.
    def _connection_loop(self):
        while self.running:

            for omx in self.omx_devices.values():

                was_connected = omx.connected

                omx.check_connection(timeout=5)

                if was_connected and not omx.connected:
                    print(
                        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [WARN] [MQTT] OMX OFFLINE: " f"{omx.omx_id}",
                        flush=True,
                    )

            time.sleep(1)


mqtt_manager = MQTTManager()
