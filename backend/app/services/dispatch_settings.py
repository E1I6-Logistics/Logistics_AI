"""실제 로봇의 배차 전압 검사를 서버 실행 중 변경한다."""

import logging
import math
from threading import RLock

from ..config import BATTERY_VOLTAGE_WINDOW_S, MIN_DISPATCH_BATTERY_VOLTAGE


class DispatchSettings:
    def __init__(self, minimum_voltage=MIN_DISPATCH_BATTERY_VOLTAGE):
        self._lock = RLock()
        self._configured_voltage = minimum_voltage
        # 기존 환경변수 0은 시작 시 OFF로 유지하고, ON으로 바꾸면 기본 기준을 사용한다.
        self._real_minimum_voltage = minimum_voltage if minimum_voltage > 0 else 11.2
        self._real_voltage_check_enabled = minimum_voltage > 0

    def snapshot(self):
        with self._lock:
            return {
                "mode": "real",
                "battery_voltage_check_enabled": self._real_voltage_check_enabled,
                "minimum_dispatch_voltage": self._real_minimum_voltage,
                "voltage_window_s": BATTERY_VOLTAGE_WINDOW_S,
            }

    def set_voltage_check(self, enabled: bool):
        with self._lock:
            if self._real_voltage_check_enabled != enabled:
                self._real_voltage_check_enabled = enabled
                logging.getLogger("uvicorn.error").info(
                    "[DISPATCH SETTINGS] real battery_voltage_check=%s minimum_voltage=%.2fV",
                    enabled, self._real_minimum_voltage,
                )
            return self.snapshot()

    def battery_block_reason(self, voltage, *, mode):
        with self._lock:
            if mode == "real":
                if not self._real_voltage_check_enabled:
                    return None
                threshold = self._real_minimum_voltage
            else:
                # 실제 로봇 토글은 시뮬레이션의 기존 환경변수 기준에 영향을 주지 않는다.
                threshold = self._configured_voltage
        if threshold <= 0:
            return None
        try:
            voltage = float(voltage)
        except (TypeError, ValueError):
            return "battery_unknown"
        if not math.isfinite(voltage) or voltage <= 0:
            return "battery_unknown"
        return "low_battery" if voltage < threshold else None


dispatch_settings = DispatchSettings()
