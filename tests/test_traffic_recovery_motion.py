"""호출 전 거리·시간 정보의 단위·누적·정차 로봇 제외와 실행 검사 보존을 확인한다."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from backend.app.services.traffic_joint_actions import JointAction, Step, TrafficSituation
from backend.app.services.traffic_metrics import DEFAULT_TRAFFIC_ESTIMATE_SPEED_MPS
from backend.app.services.traffic_recovery_dependencies import dependency_snapshot, dependency_code_choice
from backend.app.services.traffic_recovery_motion import add_motion_estimates
from backend.app.services.traffic_recovery_search import RecoveryMove
from backend.simulation.traffic_joint_actions import JointActionSimulation
from tests.test_traffic_recovery_dependencies import payload, RuleSelector


def small_scene(*, parked=False, enabled=True):
    nodes = dict(a=(0, 0), b=(3, 0), c=(0, 1), d=(1, 1), m=(1, 0))
    requests = dict(r1=dict(current_node="a", node="b", waiting_seconds=0))
    if parked:
        requests["parked"] = dict(current_node="m", node="m", waiting_seconds=0)
        moves = (
            RecoveryMove(0, 0, 2, Step("r1", "PROCEED", ("a", "m", "b"))),
            RecoveryMove(1, 0, 1, Step("parked", "YIELD", ("m", "d"))),
            RecoveryMove(1, 1, 2, Step("parked", "PROCEED", ("d", "m"))),
        )
    else:
        requests["r2"] = dict(current_node="c", node="d", waiting_seconds=0)
        moves = (RecoveryMove(0, 0, 2, Step("r1", "PROCEED", ("a", "b"))),
                 RecoveryMove(1, 0, 2, Step("r2", "PROCEED", ("c", "d"))))
    scene = SimpleNamespace(requests=requests, planner=SimpleNamespace(nodes=nodes),
                            config=dict(recovery_motion_estimates=enabled),
                            _plan=lambda key, kind, steps: JointAction(key, kind, tuple(steps), ()))
    return scene, tuple(requests), moves


def facts(scene, ids, moves, *, stages=None, steps=()):
    stages = stages or (0,) * len(ids)
    choices = [(i, None) for i, m in enumerate(moves) if stages[m.robot_index] == m.from_stage]
    return dependency_snapshot(scene, ids, stages, choices, moves, chosen_steps=steps)


def test_equal_node_counts_do_not_imply_equal_distance_and_short_request_goes_first():
    scene, ids, moves = small_scene()
    value = facts(scene, ids, moves)
    long, short = (value["offered_moves"][key]["estimated_motion"] for key in ("M1", "M2"))
    assert long["distance_m"] == 3 and short["distance_m"] == 1
    assert long["travel_seconds"] == 15 and short["travel_seconds"] == 5
    assert long["mean_goal_completion_lower_bound_seconds"] == 17.5
    assert short["mean_goal_completion_lower_bound_seconds"] == 12.5
    assert dependency_code_choice(value, can_backtrack=False) == "M2"
    scene.config["recovery_motion_estimates"] = False
    original = facts(scene, ids, moves)
    assert "motion_estimation" not in original
    assert dependency_code_choice(original, can_backtrack=False) == "M1"


def test_prefix_time_is_preserved_and_completed_request_does_not_restart_at_zero():
    scene, ids, moves = small_scene()
    value = facts(scene, ids, moves, stages=(2, 0), steps=(moves[0].step,))
    assert value["motion_estimation"]["prefix_travel_seconds"] == 15
    row = value["offered_moves"]["M2"]["estimated_motion"]
    assert row["completion_seconds"] == 20
    assert row["goal_completion_lower_bound_seconds"] == {"r1": 15, "r2": 20}
    assert row["mean_goal_completion_lower_bound_seconds"] == 17.5


def test_missing_completed_prefix_time_is_unknown_instead_of_zero():
    scene, ids, moves = small_scene()
    row = facts(scene, ids, moves, stages=(2, 0))["offered_moves"]["M2"]["estimated_motion"]
    assert row["goal_completion_lower_bound_seconds"]["r1"] is None
    assert row["mean_goal_completion_lower_bound_seconds"] is None


def test_yield_time_is_included_but_parked_return_is_not_an_extra_order_completion():
    scene, ids, moves = small_scene(parked=True)
    value = facts(scene, ids, moves)
    row = value["offered_moves"]["M2"]["estimated_motion"]
    assert row["moving_request_count"] == 1
    assert row["mean_goal_completion_lower_bound_seconds"] == 20
    assert row["all_returns_completion_lower_bound_seconds"] == 25
    assert set(row["goal_completion_lower_bound_seconds"]) == {"r1"}
    # 15초인 R1 직행보다 20초로 보이더라도 실제 길목을 비우는 양보를 먼저 검토한다.
    assert dependency_code_choice(value, can_backtrack=False) == "M2"
    yielded = facts(scene, ids, moves, stages=(0, 1), steps=(moves[1].step,))
    returned = yielded["offered_moves"]["M3"]
    assert returned["finishes_on_required_nodes"]
    assert dependency_code_choice(yielded, can_backtrack=False) == "M1"


def test_timing_basis_is_explicit_and_matches_current_simulation_speed():
    from backend.app.services.mock_data import SIMULATION_SPEED_MPS

    scene, ids, moves = small_scene()
    basis = facts(scene, ids, moves)["motion_estimation"]
    assert basis["speed_mps"] == DEFAULT_TRAFFIC_ESTIMATE_SPEED_MPS == SIMULATION_SPEED_MPS
    assert basis["completion_values_are_optimistic_lower_bounds"]
    assert basis["not_a_reservation_or_complete_plan_verdict"]
    assert not basis["includes_inference"] and not basis["includes_turns_or_reservation_delays"]
    for speed in (0, -1, True, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            add_motion_estimates({}, scene, ids, (0, 0), (), [(0, None)], moves, speed_mps=speed)


def test_code_and_model_receive_same_costs_and_model_really_gets_them_in_criteria():
    class Recording(RuleSelector):
        def select_choice(self, state, criteria, instructions, **kwargs):
            assert state["schema_version"] == "traffic-recovery-dependencies/0.2"
            assert "lower bounds" in instructions
            for key, move in state["dependency_facts"]["offered_moves"].items():
                assert json.loads(criteria[key])["estimated_motion"] == move["estimated_motion"]
            json.dumps(state, allow_nan=False)
            return super().select_choice(state, criteria, instructions, **kwargs)

    value = payload("DEPENDENCY_DFS")
    value["config"].update(recovery_motion_estimates=True, recovery_branch_policy="DEFER")
    untouched = deepcopy(value)
    code = TrafficSituation(value)
    assert value == untouched
    guided_input = deepcopy(value)
    guided_input["config"]["recovery_strategy"] = "KEV_DEPENDENCY"
    selector = Recording()
    guided = TrafficSituation(guided_input, recovery_selector=selector)
    assert selector.states
    assert [d["facts"] for d in code.search_report["dependency_decisions"]] == [d["facts"] for d in guided.search_report["dependency_decisions"]]
    for scene in (code, guided):
        assert scene.search_report["status"] == "FOUND"
        assert scene.search_report["motion_estimates_enabled"]
        result = JointActionSimulation(scene).execute(scene.decide())
        assert result["status"] == "COMPLETED" and result["capacity_violations"] == 0


def test_low_estimate_does_not_allow_a_model_to_bypass_reservation_check():
    class ProgressFirst:
        def select_choice(self, state, criteria, instructions, **kwargs):
            candidates = state["dependency_facts"]["offered_moves"]
            key = next((k for k, v in candidates.items() if v["robot_id"] == "robot1"), None)
            return {"choice": key or ("BACKTRACK" if "BACKTRACK" in criteria else next(iter(criteria)))}

    value = payload()
    value["config"]["recovery_motion_estimates"] = True
    scene = TrafficSituation(value, recovery_selector=ProgressFirst())
    assert scene.search_report["guidance_calls"][0]["check_result"] == "NO_FEASIBLE_RESERVATION"
    assert JointActionSimulation(scene).execute(scene.decide())["steps_completed"] == 0


def test_invalid_estimate_flag_is_rejected_before_search():
    value = payload()
    value["config"]["recovery_motion_estimates"] = "yes"
    with pytest.raises(ValueError, match="recovery_motion_estimates"):
        TrafficSituation(value)
