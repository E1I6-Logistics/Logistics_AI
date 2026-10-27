"""KEV로 N3/N4 컨베이어 작업대를 선택하는 보조 판단 모듈."""

from __future__ import annotations

import asyncio
import os

from ..route_selector.kev_selector import KevSelector


WORKSTATION_INSTRUCTIONS = (
    "Choose exactly one listed workstation node. All listed workstations are confirmed free by code. "
    "Prefer the shorter route distance when it is clearly shorter. When route distances are similar, prefer the workstation "
    "with fewer recent assignments to avoid concentrating work on one conveyor. Return only one listed node ID."
)


class KevWorkstationDispatcher:
    """작업대 후보 중 하나를 고른다. 점유·예약 검증과 최종 배정은 호출 코드가 담당한다."""

    def __init__(self):
        self._selector = None

    def _select(self, state, choices, question_id):
        if self._selector is None:
            timeout = float(os.getenv("KEV_WORKSTATION_TIMEOUT_SECONDS", "3"))
            self._selector = KevSelector(timeout_seconds=timeout)
        result = self._selector.select_choice(
            state=state,
            candidates=choices,
            instructions=WORKSTATION_INSTRUCTIONS,
            question_id=question_id,
        )
        choice = str(result.get("choice"))
        if choice not in choices:
            raise RuntimeError(f"KEV가 후보 밖 작업대를 선택했습니다: {choice}")
        return choice

    async def select(self, state, choices, question_id):
        return await asyncio.to_thread(self._select, state, choices, question_id)


kev_workstation_dispatcher = KevWorkstationDispatcher()
