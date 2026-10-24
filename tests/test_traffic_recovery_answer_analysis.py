"""당장 이동 가능함과 끝까지 해결 가능함을 구별하는 사후 평가를 검증한다."""

from copy import deepcopy
import json

import pytest

from backend.simulation.traffic_recovery_answer_analysis import CompletionAssessment
from backend.simulation.traffic_recovery_candidate_diagnosis import diagnosis_cases


R1_GO = dict(robot_id="robot1", action="PROCEED", path=["3", "11", "7", "9", "10", "0"])
R2_OUT = dict(robot_id="robot2", action="YIELD", path=["11", "6"])
R2_BACK = dict(robot_id="robot2", action="PROCEED", path=["6", "11"])
R3_OUT = dict(robot_id="robot3", action="YIELD", path=["10", "8"])


def evaluate(prefix, moves, choice=0, *, case="workstation3_return", positions=None):
    initial = {"robot1": "3", "robot2": "11", "robot3": "10"}
    for step in prefix:
        initial[step["robot_id"]] = step["path"][-1]
    state = dict(chosen_prefix=deepcopy(prefix), robots=[dict(robot_id=k, current_node=v) for k, v in (positions or initial).items()])
    return CompletionAssessment(diagnosis_cases()[case]).assess(state, {f"C{i}": json.dumps(move) for i, move in enumerate(moves)}, f"C{choice}")


def test_blocked_progress_is_distinct_from_yield_that_allows_complete_plan():
    result = evaluate([], [R1_GO, R2_OUT])
    assert result["category"] == "IMMEDIATE_REJECTED"
    assert result["avoidable_with_offered_choice"]
    assert result["offered_complete_choices"] == ["C1"]
    witness = result["candidates"]["C1"]["witness"]
    assert len(witness) == 5
    assert witness[-1]["path"][-1] in ("11", "10")


def test_early_restore_can_pass_immediate_check_but_lose_all_completions():
    result = evaluate([R2_OUT], [R2_BACK, R3_OUT])
    assert result["category"] == "NO_COMPLETE_PLAN"
    assert result["candidates"]["C0"]["immediate_reason"] is None
    assert result["avoidable_with_offered_choice"]
    assert result["offered_complete_choices"] == ["C1"]
    after_passage = evaluate([R2_OUT, R3_OUT, R1_GO], [R2_BACK])
    assert after_passage["category"] == "CAN_COMPLETE"


def test_no_useful_offered_choice_is_not_counted_as_avoidable_model_error():
    result = evaluate([], [R1_GO, R2_OUT], case="missing_second_yield", choice=1)
    assert result["category"] == "NO_COMPLETE_PLAN"
    assert result["offered_complete_choices"] == []
    assert not result["avoidable_with_offered_choice"]


def test_stale_positions_and_repeated_yield_are_rejected_as_bad_saved_data():
    with pytest.raises(ValueError, match="가상 위치"):
        evaluate([], [R2_OUT], positions={"robot1": "3", "robot2": "6", "robot3": "10"})
    with pytest.raises(ValueError, match="1회 양보 계약"):
        evaluate([R2_OUT, R2_BACK, R2_OUT], [R1_GO])
