"""[모듈: kev_strategy.py - KEV 기반 단일/다중 로봇 회피 전략 및 전략 순서 평가]
- 역할: KEV AI 모델에 복수 회피 전략(KEEP_PATH, REROUTE)을 질의하여 확률 기반 최적 전략 순서를 도출한다.
- 주요 기능:
  1. 전략 선택 프롬프트 지침(KEV_STRATEGY_INSTRUCTIONS) 정의
  2. 스냅샷 식별 해시 생성(fingerprint)
  3. KEV 선택 확률 기반 전략 순위 정렬(probability_order)
  4. 단일 로봇 충돌 회피 전략 생성(build_avoidance_strategies)
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

KEV_STRATEGY_INSTRUCTIONS = (
    "Choose ONE next route strategy to TEST for the robot. "
    "The returned choice probabilities will order the remaining strategies for this snapshot. "
    "KEEP_PATH uses the supplied base path; REROUTE excludes the specified resources. "
    "Reroute paths have not been verified yet. "
    "Use current occupancy, remaining paths and recent failures. "
    "Prefer strategies that safely avoid deadlocks and imminent collisions. "
    "Return only the choice criteria ID."
)


def fingerprint(value: Any) -> str:
    """임의의 파이썬 객체를 결정론적 SHA-256 해시 문자열로 변환한다."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def probability_order(result: dict[str, Any], candidates: Mapping[str, Any]) -> list[str]:
    """KEV의 선택 응답 내 각 후보별 확률(probabilities)을 기준으로 내림차순 정렬된 전략 ID 리스트를 반환한다."""
    if not isinstance(result, dict) or not isinstance(result.get("choice"), str):
        raise ValueError("KEV 전략 응답에 choice 키가 올바르지 않습니다.")
    if result["choice"] not in candidates:
        raise ValueError("KEV가 제안한 선택지가 후보군에 존재하지 않습니다.")

    probabilities = result.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != set(candidates):
        raise ValueError("KEV 전략 응답의 후보 확률 목록이 누락되었거나 일치하지 않습니다.")

    values = list(probabilities.values())
    if not all(
        not isinstance(p, bool) and isinstance(p, (int, float)) and math.isfinite(p) and 0 <= p <= 1
        for p in values
    ) or abs(sum(values) - 1.0) > 0.05:
        raise ValueError("KEV 전략 응답의 확률 값 범위가 올바르지 않습니다 (합 1.0 필요).")

    return sorted(candidates.keys(), key=lambda key: -float(probabilities[key]))


def build_avoidance_strategies(
    path: list[int | str],
    conflict_nodes: set[int | str] | None = None,
    conflict_edges: set[tuple[int | str, int | str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """기본 경로 유지(KEEP_PATH) 및 충돌 노드/간선을 회피하는 우회 전략(REROUTE) 후보군 딕셔너리를 생성한다."""
    strategies: dict[str, dict[str, Any]] = {
        "KEEP_PATH": {
            "action": "KEEP",
            "blocked_nodes": [],
            "blocked_edges": [],
        }
    }
    nodes = list(conflict_nodes or [])
    edges = list(conflict_edges or [])

    if nodes or edges:
        strategies["REROUTE_PRIMARY"] = {
            "action": "REROUTE",
            "blocked_nodes": sorted([str(n) for n in nodes]),
            "blocked_edges": sorted([[str(a), str(b)] for a, b in edges]),
        }

    return strategies


__all__ = [
    "KEV_STRATEGY_INSTRUCTIONS",
    "fingerprint",
    "probability_order",
    "build_avoidance_strategies",
]
