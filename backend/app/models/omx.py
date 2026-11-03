import json
import time

from ..services.omx_stock import PalletObservation


class OMX:
    # OMX의 초기 상태를 설정한다.
    def __init__(self, omx_id, mqtt_client):
        self.omx_id = omx_id
        self.mqtt_client = mqtt_client

        # 연결 상태
        self.connected = True
        self.last_seen = time.time()

        # 현재 작업 정보
        self.job_id = None
        self.items = {}

        self.current_count = 0
        self.total_count = 0

        # 작업 진행 상태
        self.state = "IDLE"
        self.message = ""
        self.detail = ""
        self.count_kind = None
        self.physical_unloading_verified = None
        self.pallet_observation = PalletObservation(omx_id)

    # =========================================================
    # Main -> OMX 명령
    # =========================================================

    # 명령을 전송한다.
    def send_command(self, job_id, items):
        self.job_id = job_id
        self.items = items

        self.current_count = 0
        self.total_count = sum(items.values())

        self.count_kind = None
        self.physical_unloading_verified = None
        self.message = ""
        self.state = "COMMAND_SENT"
        self.pallet_observation.clear()

        payload = {"job_id": job_id, "items": items}

        topic = f"{self.omx_id}/command"

        self._publish(topic, payload)

    # =========================================================
    # OMX -> Main 상태 처리
    # =========================================================

    # 상태를 갱신한다.
    def update_status(self, data):
        if not self.check_connection():
            self.pallet_observation.clear()
        self.connected = True
        self.last_seen = time.time()

        if "state" in data:
            self.state = data["state"]
            self.detail = str(data.get("detail", ""))[:300]
            if self.state not in ("IDLE", "READY"):
                self.pallet_observation.clear()

    # 응답을 갱신한다.
    def update_ack(self, data):
        self.connected = True
        self.last_seen = time.time()

        incoming = data.get("job_id")
        # A rejected SSH/manual order must not overwrite the running FMS job.
        if (data.get("accepted") is False and self.job_id and incoming != self.job_id
                and self.state in ("WORKING", "COMMAND_SENT")):
            return
        self.job_id = incoming

        if data.get("accepted", False):
            self.state = "WORKING"
            self.pallet_observation.clear()
        else:
            self.state = "REJECTED"

    # 진행을 갱신한다.
    def update_progress(self, data):
        self.connected = True
        self.last_seen = time.time()

        self.job_id = data.get("job_id")

        self.current_count = data.get("current", self.current_count)
        self.total_count = data.get("total", self.total_count)

        self.state = "WORKING"
        self.pallet_observation.clear()
        self.count_kind = data.get("count_kind", self.count_kind)

    # 결과를 갱신한다.
    def update_result(self, data):
        self.connected = True
        self.last_seen = time.time()

        self.job_id = data.get("job_id")

        success = data.get("success", False)

        self.message = str(data.get("message", ""))[:1000]
        self.count_kind = data.get("count_kind", self.count_kind)
        self.physical_unloading_verified = data.get("physical_unloading_verified")

        if success:
            self.state = "SUCCESS"
        else:
            self.state = "FAILED"

    # =========================================================
    # 연결 상태
    # =========================================================

    # 연결을 확인한다.
    def check_connection(self, timeout=5):
        elapsed = time.time() - self.last_seen

        if elapsed > timeout:
            self.connected = False
            self.pallet_observation.clear()

        return self.connected

    def snapshot(self):
        connected = self.check_connection()
        return {
            "omx_id": self.omx_id, "connected": connected,
            "state": self.state if connected else "OFFLINE", "detail": self.detail,
            "last_seen": self.last_seen, "job_id": self.job_id, "items": dict(self.items),
            "current_count": self.current_count, "total_count": self.total_count,
            "message": self.message, "count_kind": self.count_kind,
            "physical_unloading_verified": self.physical_unloading_verified,
            "stock": self.pallet_observation.snapshot(connected=connected, idle=self.state in ("IDLE", "READY")),
        }

    # =========================================================
    # MQTT Publish
    # =========================================================

    # 관련을 발행한다.
    def _publish(self, topic, payload):
        message = json.dumps(payload)
        self.mqtt_client.publish(topic, message)

        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [OMX:{self.omx_id}] " f"Published {topic}: {payload}",
            flush=True,
        )
