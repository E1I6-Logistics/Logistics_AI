"""[모듈: kev_client.py - Kev AI 의사결정 클라이언트 및 지침 정의]
- 역할: Kev 의사결정 API(/v1/systemone)를 호출하는 실시간 클라이언트(`LiveKEV`)와 4단계 프롬프트 지침을 관리한다.
- 기능:
  1. 1~4단계(Zone 선택, 우선순위 평가, 행동 선택, 대기 노드 선택) 프롬프트 지침 상수 정의
  2. 단일/다중 로봇 우선순위 점수 평가 (score, score_many)
  3. 선택지 기반 판단 (pick) - 후보 1개 시 API 호출 생략(forced)
  4. 우회 경로 탐색 (route) - preview_llm_route 재사용
"""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path
from typing import Any

from .kev_graph import Graph, _node

# [1단계 지침] 다중 로봇 경로에서 발견된 물리적 공유 노드/간선 중 가장 먼저 해결할 충돌 구간 선택
KEV_ZONE_INSTRUCTIONS = (
    "You are the MAPF conflict-zone selection component. "
    "Each candidate represents one shared resource or a connected group "
    "of shared nodes and edges. "
    "Choose ONE candidate zone to resolve first, preferably one that "
    "blocks imminent progress. "
    "Do not invent IDs, arrival times, or occupancy. "
    "Return only a supplied criteria ID."
)

# [2단계 지침 - Score 방식 (AX 표준)] 충돌 구간에서 로봇들의 상대적 우선순위 점수 평가 (score_many 일괄 평가)
KEV_PRIORITY_SCORE_INSTRUCTIONS = (
    "Evaluate the priority of scoring_robot relative to "
    "other robots in the current MAPF state. "
    "Consider current position, route progress, "
    "task priority, occupancy, and shared conflict zones. "
    "Use the same evaluation criteria for every robot. "
    "Higher scores indicate higher priority to proceed. "
    "Do not invent arrival times or occupancy. "
    "Do not treat the score as movement permission."
)

# [2단계 지침 - Legacy Fallback (Choice 방식)] 충돌 구간에서 두 로봇 중 먼저 통과할 우선순위 로봇(priority_robot) 결정
KEV_PRIORITY_INSTRUCTIONS = (
    "You are the MAPF priority-decision component. Choose ONE robot to have "
    "priority at the indicated shared resource, considering current position, "
    "progress, task priority and occupancy. Avoid deadlocks and repeated "
    "unnecessary concessions. Return only the criteria ID."
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
    "Return only a supplied action ID."
)

# [4단계 지침] WAIT 선택 시 충돌 구간 진입 전 안전하게 정차할 상류 대기 노드(wait_node) 선택
KEV_WAIT_INSTRUCTIONS = (
    "You are the MAPF waiting-position selection component. Choose one "
    "upstream node before the shared resource where the yielding robot should "
    "wait. Consider path continuity and whether another robot would be "
    "blocked. Physical clearance is verified separately by FMS. Return "
    "only a supplied criteria ID."
)

_PRIORITY_LEVELS = [
    "Very low priority",
    "Low priority",
    "Medium priority",
    "High priority",
    "Very high priority",
]


class LiveKEV:
    """기존 Logistics_AI의 경로 preview(`preview_llm_route`)와 `KevSelector`를 재사용하는 실시간 판단 클라이언트."""

    def __init__(self, repo: Path, *, model: str, deadline: float, max_steps: int | None):
        repo = repo.expanduser().resolve()
        target = repo / "backend/app/services/llm_route_preview.py"
        if not target.is_file():
            raise FileNotFoundError(f"Not a Logistics_AI checkout: {target}")
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))

        from backend.app.services.llm_route_preview import preview_llm_route
        from backend.app.services.route_selector.kev_selector import KevSelector

        self.selector = KevSelector(model=model)
        self.model = model
        self.preview_llm_route = preview_llm_route
        self.calls = 0
        self.model_seconds = 0.0
        self.forced = 0
        self.warnings: list[str] = []

    def score(self, state: dict[str, Any], question_id: str) -> float:
        """단일 로봇의 우선순위 점수를 평가한다."""
        result = self.selector.evaluate_score(
            state, _PRIORITY_LEVELS, KEV_PRIORITY_SCORE_INSTRUCTIONS, question_id=question_id
        )
        score = float(result["score"])
        if not math.isfinite(score):
            raise ValueError("Invalid KEV priority score")
        self.calls += 1
        self.model_seconds += float(result.get("total_duration_seconds") or 0.0)
        return score

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
        res = self.selector.select_choice(
            state, candidates, instructions, question_id=question_id
        )
        self.calls += 1
        self.model_seconds += float(res.get("total_duration_seconds") or 0.0)
        choice = str(res["choice"])
        if choice not in candidates:
            raise RuntimeError(f"KEV returned an invalid choice {choice!r}")
        return choice

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
            if traffic is not None:
                kwargs["traffic"] = traffic
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
