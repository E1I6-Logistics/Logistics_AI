"""자동 도킹 완료 시 접촉 센서를 필수로 확인할지 정한다."""

import logging
import os
from threading import RLock


class DockingSettings:
    def __init__(self):
        self._lock = RLock()
        self._contact_verification_enabled = os.getenv("FMS_DOCK_CONTACT_CHECK_ENABLED", "1").strip().lower() not in {
            "0", "false", "off", "no",
        }

    @property
    def contact_verification_enabled(self):
        with self._lock:
            return self._contact_verification_enabled

    def snapshot(self):
        with self._lock:
            return {"mode": "real", "contact_verification_enabled": self._contact_verification_enabled}

    def set_contact_verification(self, enabled: bool):
        with self._lock:
            if self._contact_verification_enabled != enabled:
                self._contact_verification_enabled = enabled
                logging.getLogger("uvicorn.error").info("[DOCKING SETTINGS] contact_verification=%s", enabled)
            return self.snapshot()


docking_settings = DockingSettings()
