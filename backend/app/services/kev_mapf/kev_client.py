"""[모듈: kev_client.py - Kev AI 의사결정 클라이언트 및 지침 정의]
- 역할: Kev 의사결정 API(/v1/systemone)를 호출하는 실시간 클라이언트(`LiveKEV`)와 Score·행동 프롬프트 지침을 관리한다.
- 기능:
  1. 우선순위 평가와 양보 행동 선택 프롬프트 지침 상수 정의
  2. 다중 로봇 우선순위 일괄 점수 평가 (score_many)
  3. 선택지 기반 판단 (pick) - 후보 1개 시 API 호출 생략(forced)
  4. 우회 경로 탐색 (route) - preview_llm_route 재사용
"""

from __future__ import annotations

import math
import os
import time
from typing import Any, Protocol

from .kev_graph import Graph, _node

# [2단계 지침 - Score 방식 (AX 표준)] 충돌 구간에서 로봇들의 상대적 우선순위 점수 평가 (score_many 일괄 평가)
KEV_PRIORITY_SCORE_INSTRUCTIONS = (
    "Evaluate the priority of scoring_robot relative to "
    "other robots in the current MAPF state. "
    "Consider current position, route progress, "
    "task priority, occupancy, and shared conflict zones. "
    "Use the same evaluation criteria for every robot. "
    "Higher scores indicate higher priority to proceed. "
    "Robots marked execution_committed are already following an approved route; "
    "consider that a yielding plan must not stop or reroute them mid-edge. "
    "Do not invent arrival times or occupancy. "
    "Do not treat the score as movement permission."
)

# [3단계 지침] 후순위(양보) 로봇이 상류에서 대기(WAIT)할지, 충돌 구간을 피해 우회(REROUTE)할지 결정
KEV_ACTION_INSTRUCTIONS = (
    "Select exactly one supplied MAPF action ID. "
    "WAIT: Prefer this when a verified upstream wait node exists. "
    "REROUTE: Prefer this when WAIT is unavailable and "
    "action_reroute is offered. "
    "REROUTE only attempts to generate a new path; it does not move a robot. "
    "HOLD: Choose when supplied evidence indicates the available "
    "alternatives are infeasible or unsafe. "
    "Do not assume rerouting is impossible without evidence. "
    "The controller validates all generated paths and falls back "
    "to HOLD if rerouting fails. "
    "All movement needs independent FMS permission. "
    "WAIT applies only at the selected shared zone, not until the priority "
    "robot completes its entire job. Unrelated robots may proceed in parallel. "
    "Return only a supplied action ID."
)

_PRIORITY_LEVELS = [
    "Very low priority",
    "Low priority",
    "Medium priority",
    "High priority",
    "Very high priority",
]


class KevPlanner(Protocol):
    """MAPF 엔진이 요구하는 판단·경로 생성 계약. 실행 권한은 없다."""

    calls: int
    model_seconds: float
    forced: int
    warnings: list[str]
    path_source: str
    reroute_source: str

    def score_many(self, state: dict, robot_ids: list[str]) -> dict[str, float]: ...

    def pick(self, state: dict, candidates: dict[str, str], instructions: str, question_id: str) -> str: ...

    def route(self, graph_data: dict, start: int, goal: int, *, selector: str = "kev",
              model: str | None = None, traffic: dict | None = None) -> list[int]: ...


class LiveKEV:
    """기존 Logistics_AI의 경로 preview(`preview_llm_route`)와 `KevSelector`를 재사용하는 실시간 판단 클라이언트."""

    path_source = "kev_preview"
    reroute_source = "kev_rerouted"

    def __init__(self, *, model: str):
        from ..llm_route_preview import preview_llm_route
        from ..route_selector.kev_selector import KevSelector

        timeout = float(os.getenv("KEV_MAPF_TIMEOUT_SECONDS", "20"))
        self.selector = KevSelector(model=model, timeout_seconds=timeout)
        self.model = model
        self.preview_llm_route = preview_llm_route
        self.calls = 0
        self.model_seconds = 0.0
        self.forced = 0
        self.warnings: list[str] = []

    def score_many(self, state: dict[str, Any], robot_ids: list[str]) -> dict[str, float]:
        """여러 로봇의 우선순위를 KEV API 요청 1회로 일괄 평가한다."""
        robot_ids = sorted(set(robot_ids))
        if not robot_ids:
            return {}

        questions = {
            f"priority_score_{rid}": {
                "type": "score",
                "instructions": f"For this question, scoring_robot is {rid}. {KEV_PRIORITY_SCORE_INSTRUCTIONS}",
                "criteria": _PRIORITY_LEVELS,
            }
            for rid in robot_ids
        }
        try:
            response = self.selector.decide(state, questions)
            scores = {}
            for rid in robot_ids:
                answer = self.selector._answer(response, f"priority_score_{rid}", "score")
                score = float(answer["score"])
                if not math.isfinite(score):
                    raise ValueError(f"Invalid KEV priority score: {rid}")
                scores[rid] = score

            self.calls += 1
            self.model_seconds += float(response.get("total_duration_seconds") or 0.0)
            return scores
        except Exception as exc:
            raise RuntimeError(f"KEV priority inference failed: {exc}") from exc

    def pick(
        self,
        state: dict[str, Any],
        candidates: dict[str, str],
        instructions: str,
        question_id: str,
    ) -> str:
        """후보가 1개면 API 호출 없이 강제 선택(`forced`)하고, 2개 이상이면 `KevSelector.select_choice()`로 판단한다."""
        if not candidates:
            raise ValueError(f"no selectable choices for {question_id}")
        if len(candidates) == 1:
            self.forced += 1
            return next(iter(candidates))
        try:
            res = self.selector.select_choice(
                state, candidates, instructions, question_id=question_id
            )
            self.calls += 1
            self.model_seconds += float(res.get("total_duration_seconds") or 0.0)
            choice = str(res["choice"])
            if choice not in candidates:
                raise RuntimeError(f"KEV returned an invalid choice {choice!r}")
            return choice
        except Exception as exc:
            raise RuntimeError(f"KEV choice inference failed: {exc}") from exc

    def route(
        self,
        graph_data: dict,
        start: int,
        goal: int,
        *,
        selector: str = "kev",
        model: str | None = None,
        traffic: dict[str, Any] | None = None,
    ) -> list[int]:
        """기존 FMS preview(`preview_llm_route`)의 카탈로그·검증을 사용해 새로운 경로/우회 경로를 생성한다."""
        started = time.perf_counter()
        try:
            kwargs: dict[str, Any] = {
                "selector": selector,
                "model": self.model if model is None else model,
            }
            result = self.preview_llm_route(
                graph_data,
                str(start),
                str(goal),
                **kwargs,
            )
            if not isinstance(result, dict) or not isinstance(result.get("path"), list):
                raise ValueError("preview_llm_route() must return a dict with a path list")
            path = [_node(n) for n in result["path"]]
            Graph(graph_data).check_path(path, start=start, goal=goal)
            self.calls += int(result.get("calls", 0))
            return path
        finally:
            self.model_seconds += time.perf_counter() - started
