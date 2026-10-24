"""시험용 ID 변환이 실제 이동·상태·검사 결과를 바꾸거나 유출하지 않는지 확인한다."""

from copy import deepcopy
import json

import pytest

from backend.app.services.traffic_joint_actions import TrafficSituation
from backend.app.services.route_service import plan_route
from backend.simulation.traffic_joint_actions import JointActionSimulation
from backend.simulation.traffic_recovery_candidate_diagnosis import (
    CONDITIONS, CandidatePresentationSelector, cyclic_labels, diagnosis_cases, replay, scene_labels,
)
from tests.test_traffic_recovery_guidance import MoveSelector


class RecordingSelector:
    name = "test"

    def select_choice(self, state, criteria, instructions, **kwargs):
        self.received = deepcopy(dict(state=state, criteria=criteria, instructions=instructions, kwargs=kwargs))
        first = next(iter(criteria))
        self.last_inference = dict(request=deepcopy(self.received))
        return dict(choice=first, probabilities={key: 1.0 if key == first else 0.0 for key in criteria})


@pytest.mark.parametrize("id_style,order", CONDITIONS)
def test_presentation_preserves_moves_state_and_maps_result_back(id_style, order):
    state = {"robots": [{"robot_id": "robot1", "current_node": "3"}], "recent_checks": []}
    criteria = {"M1": "move-one", "M3": "move-three"}
    original = deepcopy((state, criteria))
    selector = RecordingSelector()
    proxy = CandidatePresentationSelector(selector, cyclic_labels(["M1", "M2", "M3"]), id_style=id_style, order=order)
    result = proxy.select_choice(state, criteria, "same instructions", question_id="same-question")
    expected_keys = ["M3", "M1"] if order == "reverse" else ["M1", "M3"]
    assert list(selector.received["criteria"].values()) == [criteria[k] for k in expected_keys]
    assert result["choice"] == expected_keys[0]
    assert set(result["probabilities"]) == set(criteria)
    assert result["probabilities"][expected_keys[0]] == 1.0
    assert selector.received["state"] == state
    assert selector.received["instructions"] == "same instructions"
    assert selector.received["kwargs"] == {"question_id": "same-question"}
    assert (state, criteria) == original
    assert proxy.last_inference["actual_inference"] == selector.last_inference


def test_unknown_model_choice_is_rejected_without_substitution():
    class Unknown:
        def select_choice(self, *args, **kwargs):
            return {"choice": "NOT_OFFERED"}

    proxy = CandidatePresentationSelector(Unknown(), {"M1": "M2", "M2": "M1"}, id_style="shifted", order="original")
    with pytest.raises(ValueError, match="없는 모델 응답"):
        proxy.select_choice({}, {"M1": "one", "M2": "two"}, "instructions")
    with pytest.raises(ValueError, match="일대일"):
        CandidatePresentationSelector(Unknown(), {"M1": "M2", "M2": "M2"}, id_style="shifted", order="original")


def test_replay_measures_id_and_order_separately_without_sending_assessment():
    record = dict(call=1, state={"positions": "same"}, criteria={"M1": "one", "M2": "two"},
                  assessment_only_reasons={"M1": "VALIDATION_FAILURE", "M2": None})
    selector = RecordingSelector()
    report = replay([record], selector, 2)
    assert len(report["requests"]) == 8
    # 첫 항목만 고르는 대역은 이름 변경에는 불변이고 표시 순서에만 반응해야 한다.
    assert all(row["pairs"] == 2 for row in report["paired_effects"])
    assert [row["different_semantic_choice"] for row in report["paired_effects"]] == [0, 0, 2, 2]
    assert "assessment" not in json.dumps(selector.received)
    assert "VALIDATION_FAILURE" not in json.dumps(selector.received)


def test_second_placement_requires_both_blockers_and_preserves_shared_budgets():
    cases = diagnosis_cases()
    value = cases["workstation4_return"]
    for node in ("12", "10"):
        with pytest.raises(ValueError):
            plan_route("4", "0", blocked_nodes=[node])
    for case in cases.values():
        assert case["config"]["recovery_search"] == dict(max_checks=128, max_steps=8, max_seconds=10.0)
    labels = scene_labels(value)
    assert all(key != label for key, label in labels.items())
    for id_style, order in CONDITIONS:
        payload = deepcopy(value)
        payload["config"].update(recovery_strategy="KEV_GUIDED", recovery_guidance={"max_calls": 8, "request_seconds": 1.0})
        proxy = CandidatePresentationSelector(MoveSelector(), labels, id_style=id_style, order=order)
        scene = TrafficSituation(payload, recovery_selector=proxy)
        assert scene.search_report["status"] == "FOUND"
        result = JointActionSimulation(scene).execute(scene.decide())
        assert result["status"] == "COMPLETED"
        assert result["capacity_violations"] == 0
        assert result["final_nodes"] == {"robot1": "0", "robot2": "12", "robot3": "10"}
