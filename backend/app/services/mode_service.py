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
    # 기존 AI 모드 설정. KEV 배차를 먼저 적용하는 동안 경로는 기존 알고리즘을 사용한다.
    # "ai": {"route": "ai", "dispatch": "algorithm", "workstation": "algorithm"},
    "ai": {"route": "algorithm", "dispatch": "ai", "workstation": "algorithm"},
}


# 기능별 AI 또는 알고리즘 실행 방식을 결정한다.
def feature_engine(feature, operation_mode):
    return FEATURE_ENGINES[operation_mode][feature]


class RobotModeManager:
    # RobotModeManager의 초기 상태를 설정한다.
    def __init__(self, initial_mode: str) -> None:
        self._lock = RLock()
        self._mode: RobotMode = self._validate(initial_mode)
        self._operation_mode: OperationMode = "algorithm"

    # 관련을 검증한다.
    @staticmethod
    def _validate(mode: str) -> RobotMode:
        normalized = str(mode).strip().lower()
        if normalized not in {"real", "simulation"}:
            raise ValueError("mode must be either 'real' or 'simulation'")
        return normalized  # type: ignore[return-value]

    # 현재 시뮬레이션 또는 실제 모드를 반환한다.
    @property
    def mode(self) -> RobotMode:
        with self._lock:
            return self._mode

    # 현재 AI 또는 알고리즘 운영 모드를 반환한다.
    @property
    def operation_mode(self) -> OperationMode:
        with self._lock:
            return self._operation_mode

    # 모드를 설정한다.
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
