"""미룬 분기를 잃지 않고 모델 호출 한도와 예약 검사를 지키는지 검증한다."""

from copy import deepcopy

import pytest

from backend.app.services.traffic_joint_actions import TrafficSituation
from backend.app.services.traffic_recovery_dependencies import dependency_code_choice
from backend.simulation.traffic_joint_actions import JointActionSimulation
from tests.test_traffic_recovery_dependencies import payload, RuleSelector


def deferred_payload(*, budget_policy="CODE_CONTINUE", max_calls=8, case="workstation3_return", strategy="KEV_DEPENDENCY"):
    value = payload(strategy, case)
    value["config"]["recovery_branch_policy"] = "DEFER"
    if strategy == "KEV_DEPENDENCY":
        value["config"]["recovery_guidance_budget_policy"] = budget_policy
        value["config"]["recovery_guidance"]["max_calls"] = max_calls
    return value


class PostponeSelector:
    name = "test"

    def __init__(self, *, defer_first_calls=None):
        self.calls = []
        self.defer_first_calls = defer_first_calls

    def select_choice(self, state, criteria, instructions, **kwargs):
        self.calls.append(deepcopy((state, criteria, instructions)))
        assert state["planning_control"]["backtrack_effect"] == "POSTPONE_WITHOUT_DELETION"
        if "BACKTRACK" in criteria and (self.defer_first_calls is None or len(self.calls) <= self.defer_first_calls):
            assert "keeping" in criteria["BACKTRACK"]
            return {"choice": "BACKTRACK"}
        return {"choice": dependency_code_choice(state["dependency_facts"], can_backtrack="BACKTRACK" in criteria)}


def test_untried_moves_survive_postpone_and_resumed_branch_cannot_postpone_again():
    selector = PostponeSelector(defer_first_calls=4)
    scene = TrafficSituation(deferred_payload(max_calls=32, budget_policy="STOP"), recovery_selector=selector)
    report = scene.search_report
    assert report["status"] == "FOUND"
    assert report["deferred_branches"] > 0 and report["resumed_branches"] > 0
    assert report["code_after_model_budget_decisions"] == 0
    postponed = set()
    for number, (state, criteria, _) in enumerate(selector.calls, 1):
        prefix = tuple((s["robot_id"], s["action"], tuple(s["path"])) for s in state["chosen_prefix"])
        if prefix in postponed:
            assert "BACKTRACK" not in criteria
        if "BACKTRACK" in criteria and number <= 4:
            postponed.add(prefix)
    result = JointActionSimulation(scene).execute(scene.decide())
    assert result["status"] == "COMPLETED" and result["capacity_violations"] == 0


def test_code_continuation_is_explicit_and_never_adds_model_requests_after_budget():
    selector = PostponeSelector()
    scene = TrafficSituation(deferred_payload(max_calls=2), recovery_selector=selector)
    report = scene.search_report
    assert report["status"] == "FOUND"
    assert len(selector.calls) == report["model_calls"] == 2
    assert report["code_after_model_budget_decisions"] > 0
    assert any(d["source"] == "CODE_AFTER_MODEL_BUDGET" for d in report["dependency_decisions"])
    decision = scene.decide()
    assert decision.selection_source == "HYBRID_TEST_CODE_SEARCH"
    result = JointActionSimulation(scene).execute(decision)
    assert result["status"] == "COMPLETED" and result["steps_completed"] == 5


def test_stop_policy_keeps_pending_branches_but_does_not_execute_partial_plan():
    scene = TrafficSituation(deferred_payload(max_calls=2, budget_policy="STOP"), recovery_selector=PostponeSelector())
    report = scene.search_report
    assert report["status"] == "MODEL_CALL_LIMIT"
    assert report["pending_deferred_branches"] > 0
    assert report["code_after_model_budget_decisions"] == 0
    assert JointActionSimulation(scene).execute(scene.decide())["steps_completed"] == 0


def test_continuation_still_obeys_total_check_budget():
    value = deferred_payload(max_calls=1)
    value["config"]["recovery_search"]["max_checks"] = 3
    scene = TrafficSituation(value, recovery_selector=PostponeSelector())
    assert scene.search_report["status"] == "CHECK_LIMIT"
    assert scene.search_report["checks"] == 3
    assert scene.search_report["model_calls"] == 1
    assert JointActionSimulation(scene).execute(scene.decide())["steps_completed"] == 0


def test_invalid_model_answer_does_not_trigger_code_continuation():
    class Invalid:
        def select_choice(self, *args, **kwargs):
            return {"choice": "UNKNOWN"}

    scene = TrafficSituation(deferred_payload(), recovery_selector=Invalid())
    assert scene.search_report["status"] == "INVALID_MODEL_CHOICE"
    assert scene.search_report["code_after_model_budget_decisions"] == 0
    assert scene.search_report["checks"] == 0


@pytest.mark.parametrize("strategy", ["DEPENDENCY_DFS", "KEV_DEPENDENCY"])
def test_both_methods_exhaust_missing_supplied_action_without_executing(strategy):
    scene = TrafficSituation(deferred_payload(strategy=strategy, case="missing_second_yield", max_calls=2),
                             recovery_selector=PostponeSelector())
    assert scene.search_report["status"] == "EXHAUSTED"
    assert scene.search_report["pending_deferred_branches"] == 0
    assert JointActionSimulation(scene).execute(scene.decide())["steps_completed"] == 0


def test_invalid_policy_combinations_are_rejected_and_default_stays_discard():
    for overrides in ({"recovery_branch_policy": "TYPO"},
                      {"recovery_branch_policy": "DISCARD", "recovery_guidance_budget_policy": "CODE_CONTINUE"},
                      {"recovery_strategy": "BREADTH_FIRST"}):
        value = deferred_payload()
        value["config"].update(overrides)
        with pytest.raises(ValueError):
            TrafficSituation(value, recovery_selector=RuleSelector())
    default = TrafficSituation(payload("DEPENDENCY_DFS"))
    assert default.search_report["branch_policy"] == "DISCARD"
    assert default.search_report["checks"] == 5
