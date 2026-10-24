"""KEV 제안과 공통 검사 사이의 경계·실패 피드백·호출 한도를 검증한다."""

from copy import deepcopy
import json
from threading import Event
import unittest

from backend.app.services.traffic_joint_actions import TrafficSituation
from backend.simulation.traffic_joint_actions import JointActionSimulation
from tests.test_traffic_recovery_search import search_payload


def guided_payload():
    payload = search_payload()
    payload["config"].update(recovery_strategy="KEV_GUIDED",
                             recovery_guidance={"max_calls": 8, "request_seconds": 1.0})
    payload["config"]["recovery_search"]["max_seconds"] = 10.0
    return payload


class MoveSelector:
    name = "test"

    def __init__(self, *, fail_first=False):
        self.states = []
        self.fail_first = fail_first

    def select_choice(self, state, criteria, instructions, **kwargs):
        self.states.append(deepcopy(state))
        self.last_criteria = deepcopy(criteria)
        self.last_instructions = instructions
        robots = {r["robot_id"]: r for r in state["robots"]}
        choices = [(key, json.loads(value)) for key, value in criteria.items()]
        if self.fail_first and len(self.states) == 1:
            return {"choice": next(key for key, move in choices if robots[move["robot_id"]]["has_movement_request"])}
        # 테스트 대역은 입력에 나타난 점유와 동작 종류만 보고 순서를 정한다.
        for key, move in choices:
            other_nodes = {r["current_node"] for rid, r in robots.items() if rid != move["robot_id"]}
            if not other_nodes.intersection(move["path"][1:]) and move["action"] == "YIELD":
                return {"choice": key}
        for key, move in choices:
            if robots[move["robot_id"]]["has_movement_request"]:
                return {"choice": key}
        return {"choice": choices[0][0]}


class RecoveryGuidanceTests(unittest.TestCase):
    def test_model_suggests_unchecked_move_then_receives_failure_feedback(self):
        selector = MoveSelector(fail_first=True)
        scene = TrafficSituation(guided_payload(), recovery_selector=selector)
        report = scene.search_report
        self.assertEqual(report["status"], "FOUND")
        self.assertEqual(report["guidance_source"], "TEST_DOUBLE")
        self.assertEqual(scene.decide().selection_source, "TEST_GUIDED_SEARCH")
        self.assertGreater(report["model_calls"], 1)
        self.assertIn("NO_FEASIBLE_RESERVATION", report["rejected_checks"])
        self.assertEqual(selector.states[0]["recent_checks"], [])
        self.assertTrue(any(c["result"] == "NO_FEASIBLE_RESERVATION" for c in selector.states[1]["recent_checks"]))
        self.assertTrue(selector.states[0]["planning_only"])
        self.assertIn("NOT passed", selector.last_instructions)
        self.assertEqual(report["found_steps"], 5)
        self.assertEqual(report["guidance_calls"][0]["check_result"], "NO_FEASIBLE_RESERVATION")
        result = JointActionSimulation(scene).execute(scene.decide())
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["capacity_violations"], 0)

    def test_depth_first_baseline_uses_same_moves_and_limits_without_model(self):
        value = guided_payload()
        code_value = deepcopy(value)
        code_value["config"]["recovery_strategy"] = "DEPTH_FIRST"
        code = TrafficSituation(code_value)
        guided = TrafficSituation(value, recovery_selector=MoveSelector())
        self.assertEqual(code.search_report["limits"], guided.search_report["limits"])
        self.assertEqual(code.search_report["scope"], guided.search_report["scope"])
        self.assertEqual(code.search_report["model_calls"], 0)
        self.assertEqual(code.search_report["status"], "FOUND")
        for scene in (code, guided):
            self.assertEqual(JointActionSimulation(scene).execute(scene.decide())["status"], "COMPLETED")

    def test_unknown_choice_model_error_and_call_limit_do_not_move(self):
        class Invalid:
            def select_choice(self, *args, **kwargs):
                return {"choice": "NOT_OFFERED"}

        class Broken:
            def select_choice(self, *args, **kwargs):
                raise RuntimeError("시험 오류")

        for selector, expected in ((Invalid(), "INVALID_MODEL_CHOICE"), (Broken(), "MODEL_ERROR"), (MoveSelector(), "MODEL_CALL_LIMIT")):
            with self.subTest(expected=expected):
                value = guided_payload()
                if expected == "MODEL_CALL_LIMIT":
                    value["config"]["recovery_guidance"]["max_calls"] = 1
                scene = TrafficSituation(value, recovery_selector=selector)
                self.assertEqual(scene.search_report["status"], expected)
                self.assertEqual(set(scene.plans), {"HOLD_ALL"})
                result = JointActionSimulation(scene).execute(scene.decide())
                self.assertEqual(result["steps_completed"], 0)
                self.assertEqual(result["final_nodes"], {"robot1": "3", "robot2": "11", "robot3": "10"})

    def test_timeout_discards_late_response_and_never_checks_or_moves_it(self):
        release, finished = Event(), Event()

        class Slow:
            def select_choice(self, state, criteria, *args, **kwargs):
                release.wait(1)
                finished.set()
                return {"choice": next(iter(criteria))}

        value = guided_payload()
        value["config"]["recovery_guidance"]["request_seconds"] = 0.02
        try:
            scene = TrafficSituation(value, recovery_selector=Slow())
            self.assertEqual(scene.search_report["status"], "MODEL_TIMEOUT")
            self.assertEqual(scene.search_report["checks"], 0)
        finally:
            release.set()
            self.assertTrue(finished.wait(1))
        self.assertEqual(set(scene.plans), {"HOLD_ALL"})
        self.assertEqual(scene.search_report["checks"], 0)

    def test_cancellation_during_model_call_rejects_returned_proposal(self):
        stopped = Event()

        class Stop:
            def select_choice(self, state, criteria, *args, **kwargs):
                stopped.set()
                return {"choice": next(iter(criteria))}

        scene = TrafficSituation(guided_payload(), recovery_selector=Stop(), recovery_cancelled=stopped.is_set)
        self.assertEqual(scene.search_report["status"], "CANCELLED")
        self.assertEqual(scene.search_report["checks"], 0)
        self.assertEqual(set(scene.plans), {"HOLD_ALL"})

    def test_one_remaining_move_is_checked_without_model_request(self):
        value = guided_payload()
        # 양보 경로를 없애면 주어진 동작이 R1 진행 하나다. 검사는 실패하되 호출은 필요 없다.
        value["config"]["yield_paths"] = {}
        selector = MoveSelector()
        scene = TrafficSituation(value, recovery_selector=selector)
        self.assertEqual(scene.search_report["status"], "EXHAUSTED")
        self.assertEqual(scene.search_report["model_calls"], 0)
        self.assertEqual(selector.states, [])
        self.assertEqual(scene.search_report["checks"], 1)

    def test_invalid_strategy_and_guidance_limits_are_rejected(self):
        for options in ({"max_calls": 0}, {"max_calls": True}, {"request_seconds": 0}, {"unknown": 1}):
            value = guided_payload()
            value["config"]["recovery_guidance"] = options
            with self.subTest(options=options), self.assertRaises(ValueError):
                TrafficSituation(value, recovery_selector=MoveSelector())
        value = guided_payload()
        value["config"]["recovery_strategy"] = "UNKNOWN"
        with self.assertRaises(ValueError):
            TrafficSituation(value)

    def test_comparison_keeps_common_input_hash_and_limits(self):
        from backend.simulation.traffic_recovery_comparison import comparison_cases, run_comparison

        report = run_comparison(comparison_cases(), selector_factory=MoveSelector)
        self.assertFalse(report["live_fms_commands_sent"])
        self.assertEqual(len(report["runs"]), 9)
        for case in report["inputs"]:
            rows = [r for r in report["runs"] if r["case"] == case]
            self.assertEqual(len({r["common_input_sha256"] for r in rows}), 1)
            self.assertTrue(all(r["search"]["limits"] == rows[0]["search"]["limits"] for r in rows))
            self.assertTrue(all(r["result"]["capacity_violations"] == 0 for r in rows))


if __name__ == "__main__":
    unittest.main()
