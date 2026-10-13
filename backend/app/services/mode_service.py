"""FMS robot operating mode shared by HTTP and WebSocket handlers."""

from __future__ import annotations

from threading import RLock
from typing import Literal

from ..config import ROBOT_MODE

RobotMode = Literal["real", "simulation"]
OperationMode = Literal["algorithm", "ai"]

# Each feature can adopt AI independently as its implementation becomes ready.
# Traffic and safety continue to use the existing algorithms in both modes.
FEATURE_ENGINES = {
    "algorithm": {"route": "algorithm", "dispatch": "algorithm", "workstation": "algorithm"},
    "ai": {"route": "ai", "dispatch": "algorithm", "workstation": "algorithm"},
}


def feature_engine(feature, operation_mode):
    return FEATURE_ENGINES[operation_mode][feature]


class RobotModeManager:
    def __init__(self, initial_mode: str) -> None:
        self._lock = RLock()
        self._mode: RobotMode = self._validate(initial_mode)
        self._operation_mode: OperationMode = "algorithm"

    @staticmethod
    def _validate(mode: str) -> RobotMode:
        normalized = str(mode).strip().lower()
        if normalized not in {"real", "simulation"}:
            raise ValueError("mode must be either 'real' or 'simulation'")
        return normalized  # type: ignore[return-value]

    @property
    def mode(self) -> RobotMode:
        with self._lock:
            return self._mode

    @property
    def operation_mode(self) -> OperationMode:
        with self._lock:
            return self._operation_mode

    def set_mode(self, mode: str, operation_mode: str | None = None) -> RobotMode:
        from .scenario_service import scenario_manager
        scenario_manager.ensure_available()
        normalized = self._validate(mode)
        if operation_mode is not None and operation_mode not in FEATURE_ENGINES:
            raise ValueError("operation_mode must be either 'algorithm' or 'ai'")
        with self._lock:
            self._mode = normalized
            if operation_mode is not None:
                self._operation_mode = operation_mode
            return self._mode


mode_manager = RobotModeManager(ROBOT_MODE)
