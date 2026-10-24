"""복구 탐색의 다음 검사 동작을 선택한다. 로봇 명령·예약 변경은 하지 않는다."""

from copy import deepcopy
from dataclasses import dataclass
import json
import math
from queue import Empty, Queue
from threading import Thread
from time import monotonic

from .traffic_recovery_input import INPUT_STYLES, format_guidance_state


INSTRUCTIONS = (
    "Choose ONE next move to TEST while planning a complete traffic recovery. "
    "Candidates have NOT passed occupancy or reservation checks. Code will test your choice "
    "and provide recent check results on the next request. A rejected move is not executed. "
    "Find a sequence that delivers every moving robot to its original goal and restores "
    "parked robots after temporary yields. Use current hypothetical positions and all candidate paths. "
    "Clear robots blocking a needed path before trying that path; avoid restoring a blocker "
    "before the robot needing passage has passed. If a branch fails, explore other allowed moves. "
    "This is planning only: no robot moves until a complete plan passes code validation. "
    "Do not assume the candidate order or ID indicates the answer."
)


@dataclass(frozen=True)
class GuidanceLimits:
    max_calls: int = 8
    request_seconds: float = 2.0
    input_style: str = "original"

    @classmethod
    def parse(cls, options):
        if not isinstance(options, dict) or set(options) - {"max_calls", "request_seconds", "input_style"}:
            raise ValueError("KEV 탐색 제안 한도 설정이 올바르지 않습니다.")
        values = {**cls().__dict__, **options}
        calls, seconds = values["max_calls"], values["request_seconds"]
        if isinstance(calls, bool) or not isinstance(calls, int) or not 1 <= calls <= 32:
            raise ValueError("max_calls는 1~32 정수여야 합니다.")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= 10:
            raise ValueError("request_seconds는 0초 초과 10초 이하여야 합니다.")
        if values["input_style"] not in INPUT_STYLES:
            raise ValueError("input_style은 original, occupancy_facts, compact 중 하나여야 합니다.")
        return cls(**values)


def bounded_choice(selector, state, criteria, *, timeout, cancelled, instructions=INSTRUCTIONS):
    """전체 모델 호출을 제한하고 늦은 응답을 버린다. 진행 중 HTTP는 종료될 수 있다."""
    if cancelled():
        return "CANCELLED", None
    if timeout <= 0:
        return "MODEL_TIMEOUT", None
    result = Queue(maxsize=1)

    def call():
        try:
            result.put((True, selector.select_choice(state, criteria, instructions,
                                                    question_id="traffic_recovery_move")))
        except Exception as exc:
            result.put((False, exc))

    deadline = monotonic() + timeout
    Thread(target=call, daemon=True).start()
    while True:
        if cancelled():
            return "CANCELLED", None
        remaining = deadline - monotonic()
        if remaining <= 0:
            return "MODEL_TIMEOUT", None
        try:
            ok, value = result.get(timeout=min(0.05, remaining))
        except Empty:
            continue
        if cancelled():
            return "CANCELLED", None
        if monotonic() >= deadline:
            return "MODEL_TIMEOUT", None
        return ("RESPONSE", value) if ok else ("MODEL_ERROR", f"{type(value).__name__}: {value}")


class RecoveryGuidance:
    def __init__(self, selector, options, *, cancelled):
        self.limits = GuidanceLimits.parse(options)
        if selector is None:
            raise ValueError("KEV 전략 탐색에는 제안 선택기가 필요합니다.")
        self.selector, self.cancelled = selector, cancelled
        self.calls = []

    def choose(self, scene, robot_ids, stages, steps, choices, moves, *, attempts, remaining_seconds, remaining_checks):
        if len(self.calls) >= self.limits.max_calls:
            return "MODEL_CALL_LIMIT", None
        positions = {}
        for rid, stage in zip(robot_ids, stages):
            request = scene.requests[rid]
            if stage == 0:
                positions[rid] = request["current_node"]
            elif stage == 2:
                positions[rid] = request["node"]
            else:
                positions[rid] = next(m.step.path[-1] for m in moves if m.step.robot_id == rid and m.step.action == "YIELD")
        state = dict(
            schema_version="traffic-recovery-guidance/0.1", planning_only=True,
            robots=[dict(robot_id=rid, current_node=positions[rid], original_start=scene.requests[rid]["current_node"],
                         original_goal=scene.requests[rid]["node"],
                         has_movement_request=scene.requests[rid]["current_node"] != scene.requests[rid]["node"],
                         stage=("INITIAL", "YIELDED", "FINISHED")[stage]) for rid, stage in zip(robot_ids, stages)],
            chosen_prefix=[dict(robot_id=s.robot_id, action=s.action, path=list(s.path)) for s in steps],
            recent_checks=deepcopy(attempts[-8:]), wait_allowed_nodes=sorted(scene.wait_nodes),
            blocked_nodes=sorted(scene.blocked_nodes), blocked_edges=[list(e) for e in sorted(scene.blocked_edges)],
            remaining_checks=remaining_checks, remaining_seconds=max(0, remaining_seconds),
        )
        criteria = {}
        for index, _ in choices:
            move = moves[index]
            criteria[f"M{index + 1}"] = json.dumps(dict(
                robot_id=move.step.robot_id, action=move.step.action, path=list(move.step.path),
                purpose="RESTORE_ORIGINAL_GOAL" if move.from_stage == 1 else move.step.action,
            ), ensure_ascii=False)
        model_state = format_guidance_state(state, criteria, self.limits.input_style)
        before = getattr(self.selector, "last_inference", None)
        started = monotonic()
        record = dict(call=len(self.calls) + 1, request=dict(
            model=getattr(self.selector, "api_model", None), state=model_state,
            questions={"traffic_recovery_move": dict(type="choice", instructions=INSTRUCTIONS, criteria=criteria)},
        ))
        record["input_style"] = self.limits.input_style
        if self.limits.input_style != "original":
            record["planning_state"] = deepcopy(state)
        self.calls.append(record)
        status, result = bounded_choice(self.selector, model_state, criteria,
                                        timeout=min(self.limits.request_seconds, remaining_seconds), cancelled=self.cancelled)
        record.update(status=status, elapsed_seconds=monotonic() - started)
        if status != "RESPONSE":
            if result is not None:
                record["error"] = str(result)
            return status, None
        record["result"] = deepcopy(result)
        inference = getattr(self.selector, "last_inference", None)
        if inference is not before:
            record["inference"] = deepcopy(inference)
        choice = result.get("choice") if isinstance(result, dict) else None
        if not isinstance(choice, str) or choice not in criteria:
            record.update(status="INVALID_MODEL_CHOICE", error="허용되지 않은 다음 이동 제안")
            return "INVALID_MODEL_CHOICE", None
        record["proposed_move"] = json.loads(criteria[choice])
        return "RESPONSE", int(choice[1:]) - 1
