"""관계 정보·계획 분기 복귀·동일 기능 대조와 기존 흐름 보존을 검증한다."""

from copy import deepcopy
from threading import Event

import pytest

from backend.app.services.traffic_joint_actions import TrafficSituation
from backend.app.services.traffic_recovery_dependencies import dependency_code_choice, dependency_snapshot
from backend.app.services.traffic_recovery_search import allowed_moves
from backend.simulation.traffic_joint_actions import JointActionSimulation
from backend.simulation.traffic_recovery_candidate_diagnosis import diagnosis_cases


def payload(strategy="KEV_DEPENDENCY", case="workstation3_return"):
    value = deepcopy(diagnosis_cases()[case])
    value["config"].update(recovery_strategy=strategy, recovery_guidance=dict(max_calls=8, request_seconds=1.0))
    return value


class RuleSelector:
    name = "test"

    def __init__(self, *, early_restore=False):
        self.states = []
        self.early_restore = early_restore

    def select_choice(self, state, criteria, instructions, **kwargs):
        self.states.append(deepcopy(state))
        facts = state["dependency_facts"]
        if self.early_restore and len(self.states) == 2:
            for key, move in facts["offered_moves"].items():
                if move["purpose"] == "RESTORE_ORIGINAL_GOAL":
                    return {"choice": key}
        return {"choice": dependency_code_choice(facts, can_backtrack="BACKTRACK" in criteria)}


def facts_at(value, stages):
    raw = deepcopy(value)
    raw["config"].pop("recovery_search")
    scene = TrafficSituation(raw)
    ids = tuple(sorted(scene.requests, key=lambda rid: (scene.requests[rid]["request_order"], rid)))
    moves = allowed_moves(scene, ids)
    current = dict(zip(ids, stages))
    choices = [(i, None) for i, move in enumerate(moves) if current[move.step.robot_id] == move.from_stage]
    return dependency_snapshot(scene, ids, stages, choices, moves)


def test_relations_explain_current_blockage_and_early_restore_without_oracle():
    value = payload()
    # 요청 순서상 R2, R3, R1 순으로 상태를 지정한다.
    facts = facts_at(value, (0, 1, 0))
    progress = next(v for v in facts["offered_moves"].values() if v["robot_id"] == "robot1")
    restore = next(v for v in facts["offered_moves"].values() if v["robot_id"] == "robot3")
    assert progress["current_other_occupants"] == [dict(robot_id="robot2", node="11")]
    assert restore["finishes_on_required_nodes"] == [dict(robot_id="robot1", node="10")]
    assert "witness" not in str(facts) and "CAN_COMPLETE" not in str(facts)
    assert not facts["irreversible_blockers"]
    bad_branch = facts_at(value, (0, 2, 0))
    assert bad_branch["irreversible_blockers"] == [dict(blocker="robot3", node="10", blocked_robot="robot1")]
    assert dependency_code_choice(bad_branch, can_backtrack=True) == "BACKTRACK"


def test_available_detour_prevents_false_mandatory_node_claim():
    value = payload()
    value["config"]["reroute_paths"] = {"robot1": ["3", "11", "12", "8", "10", "0"]}
    facts = facts_at(value, (0, 0, 0))
    robot = next(r for r in facts["robots"] if r["robot_id"] == "robot1")
    assert "9" not in robot["must_visit_nodes"]
    assert {"11", "10", "0"} <= set(robot["must_visit_nodes"])
    assert len(robot["remaining_supplied_paths"]) == 2


@pytest.mark.parametrize("strategy", ["DEPENDENCY_DFS", "KEV_DEPENDENCY"])
def test_both_methods_use_relations_and_execute_only_a_complete_checked_plan(strategy):
    selector = RuleSelector()
    scene = TrafficSituation(payload(strategy), recovery_selector=selector)
    report = scene.search_report
    assert report["status"] == "FOUND" and report["found_steps"] == 5
    assert report["checks"] == 5
    assert report["limits"] == dict(max_checks=128, max_steps=8, max_seconds=10.0)
    assert report["dependency_decisions"][0]["facts"]["robots"]
    assert report["duplicate_pruning"] == "DISABLED_FOR_VOLUNTARILY_ABANDONED_BRANCHES"
    result = JointActionSimulation(scene).execute(scene.decide())
    assert result["status"] == "COMPLETED" and result["capacity_violations"] == 0
    assert result["final_nodes"] == {"robot1": "0", "robot2": "11", "robot3": "10"}
    if strategy == "DEPENDENCY_DFS":
        assert not selector.states and report["model_calls"] == 0


def test_model_backtrack_discards_only_hypothetical_branch_and_can_find_another():
    selector = RuleSelector(early_restore=True)
    scene = TrafficSituation(payload(), recovery_selector=selector)
    report = scene.search_report
    assert report["status"] == "FOUND"
    assert report["voluntary_backtracks"]
    assert any(c.get("proposed_control") == "BACKTRACK" for c in report["guidance_calls"])
    assert any(c.get("proposed_move", {}).get("finishes_on_required_nodes") for c in report["guidance_calls"])
    # 실제 실행 계획에는 잘못된 조기 복귀를 포함하지 않는다.
    steps = scene.checked_plan(scene.decide()).steps
    assert [s.action for s in steps[:2]] == ["YIELD", "YIELD"]
    assert JointActionSimulation(scene).execute(scene.decide())["status"] == "COMPLETED"


def test_root_backtrack_unknown_response_and_call_limit_do_not_execute():
    class BacktrackAtRoot:
        def select_choice(self, state, criteria, instructions, **kwargs):
            assert "BACKTRACK" not in criteria
            return {"choice": "BACKTRACK"}

    for selector, expected in ((BacktrackAtRoot(), "INVALID_MODEL_CHOICE"), (RuleSelector(), "MODEL_CALL_LIMIT")):
        value = payload()
        if expected == "MODEL_CALL_LIMIT":
            value["config"]["recovery_guidance"]["max_calls"] = 1
        scene = TrafficSituation(value, recovery_selector=selector)
        assert scene.search_report["status"] == expected
        assert JointActionSimulation(scene).execute(scene.decide())["steps_completed"] == 0


def test_new_mode_discards_late_model_answer():
    release, finished = Event(), Event()

    class Slow:
        def select_choice(self, state, criteria, *args, **kwargs):
            release.wait(1)
            finished.set()
            return {"choice": next(iter(criteria))}

    value = payload()
    value["config"]["recovery_guidance"]["request_seconds"] = 0.02
    try:
        scene = TrafficSituation(value, recovery_selector=Slow())
        assert scene.search_report["status"] == "MODEL_TIMEOUT"
        assert scene.search_report["checks"] == 0
    finally:
        release.set()
        assert finished.wait(1)
    assert JointActionSimulation(scene).execute(scene.decide())["steps_completed"] == 0


def test_movement_check_is_still_enforced_for_a_bad_model_proposal():
    class AlwaysProgress:
        def select_choice(self, state, criteria, instructions, **kwargs):
            for key, move in state["dependency_facts"]["offered_moves"].items():
                if move["robot_id"] == "robot1":
                    return {"choice": key}
            return {"choice": "BACKTRACK" if "BACKTRACK" in criteria else next(iter(criteria))}

    scene = TrafficSituation(payload(), recovery_selector=AlwaysProgress())
    first = scene.search_report["guidance_calls"][0]
    assert first["check_result"] == "NO_FEASIBLE_RESERVATION"
    assert scene.search_report["status"] == "BRANCHES_ABANDONED"
    assert not scene.plans.get("RECOVERY_KEV_DEPENDENCY")
    assert JointActionSimulation(scene).execute(scene.decide())["steps_completed"] == 0


def test_pair_receives_identical_facts_and_missing_action_stays_unresolved():
    value = payload("DEPENDENCY_DFS")
    code = TrafficSituation(value)
    value["config"]["recovery_strategy"] = "KEV_DEPENDENCY"
    guided = TrafficSituation(value, recovery_selector=RuleSelector())
    assert [d["facts"] for d in code.search_report["dependency_decisions"]] == [d["facts"] for d in guided.search_report["dependency_decisions"]]
    for strategy in ("DEPENDENCY_DFS", "KEV_DEPENDENCY"):
        scene = TrafficSituation(payload(strategy, "missing_second_yield"), recovery_selector=RuleSelector())
        result = JointActionSimulation(scene).execute(scene.decide())
        assert result["status"] == "UNRESOLVED" and result["steps_completed"] == 0


def test_old_dfs_behavior_is_unchanged():
    scene = TrafficSituation(payload("DEPTH_FIRST"))
    assert scene.search_report["checks"] == 13 and scene.search_report["status"] == "FOUND"
    assert not scene.search_report["dependency_mode"]
    assert scene.search_report["dependency_decisions"] == []
