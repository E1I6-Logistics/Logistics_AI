"""같은 후보 비교, 서로 다른 기준, 실패와 모델의 불리한 선택 보존을 확인한다."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from backend.app.services.traffic_joint_actions import CHOICE_INSTRUCTIONS, TrafficSituation
from backend.simulation.traffic_joint_actions import TestSelector as FixedChoiceSelector
from backend.simulation.traffic_selection_comparison import (
    PresentedSelector, load_cases, main, run_trial, summarize,
)


class SelectionComparisonTests(unittest.TestCase):
    def setUp(self):
        self.cases = load_cases()

    def test_request_order_and_smallest_mean_are_different_rules(self):
        case = self.cases["three_robot_different_distances"]
        scene = TrafficSituation(deepcopy(case["payload"]))
        fifo = scene.decide(strategy="request_order")
        calculated = scene.decide(strategy="min_mean_completion")
        self.assertEqual(fifo.joint_action_id, "A1")
        self.assertEqual(calculated.joint_action_id, "A2")
        self.assertEqual(fifo.model_result["selection_objective"], "REQUEST_ORDER_LEXICOGRAPHIC")
        self.assertEqual(calculated.model_result["selection_objective"], "MIN_MEAN_GOAL_COMPLETION_V1")
        self.assertLess(scene.estimates["A2"]["mean_goal_completion_seconds"],
                        scene.estimates["A1"]["mean_goal_completion_seconds"])

    def test_average_goal_priority_can_choose_longer_complete_sequence(self):
        scene = TrafficSituation(deepcopy(self.cases["kev_joint_yield_or_reroute"]["payload"]))
        decision = scene.decide(strategy="min_mean_completion")
        self.assertEqual(decision.joint_action_id, "B_YIELD_robot2")
        self.assertGreater(scene.estimates[decision.joint_action_id]["all_steps_completion_seconds"],
                           scene.estimates["B_REROUTE"]["all_steps_completion_seconds"])
        self.assertIn("Do NOT prefer total distance", CHOICE_INSTRUCTIONS)

    def test_all_strategies_get_same_snapshot_and_candidates(self):
        case = self.cases["three_robot_different_distances"]
        before = deepcopy(case)
        rows = [run_trial(case, strategy, selector=FixedChoiceSelector("A2"))
                for strategy in ("request_order", "min_mean_completion", "kev")]
        self.assertEqual(len({r["snapshot_hash"] for r in rows}), 1)
        self.assertTrue(all(r["candidate_estimates"] == rows[0]["candidate_estimates"] for r in rows))
        self.assertTrue(all(r["status"] == "COMPLETED" and r["capacity_violations"] == 0 for r in rows))
        self.assertEqual(case, before)

    def test_worse_model_choice_is_preserved_and_reported(self):
        row = run_trial(self.cases["kev_joint_yield_or_reroute"], "kev",
                        selector=FixedChoiceSelector("B_REROUTE"))
        self.assertEqual(row["selected_id"], "B_REROUTE")
        self.assertFalse(row["matched_best_offered_mean"])
        self.assertGreater(row["excess_estimated_mean_seconds"], 0)

    def test_reversed_presentation_keeps_the_same_offered_plans(self):
        captured = []

        class Recorder(FixedChoiceSelector):
            def select_choice(self, state, candidates, instructions, **kwargs):
                captured.append(candidates)
                return {"choice": "A1"}

        candidates = {"A1": "first", "A2": "second", "A3": "third"}
        PresentedSelector(Recorder(), False).select_choice({}, candidates, "objective")
        PresentedSelector(Recorder(), True).select_choice({}, candidates, "objective")
        self.assertEqual(list(captured[1]), ["A3", "A2", "A1"])
        self.assertEqual(captured[0], captured[1])

    def test_hold_stays_unresolved_and_does_not_call_selector(self):
        selector = FixedChoiceSelector()
        row = run_trial(self.cases["kev_joint_hold"], "kev", selector=selector)
        self.assertEqual(selector.calls, 0)
        self.assertEqual(row["kev_requests"], 0)
        self.assertEqual(row["status"], "UNRESOLVED")
        summary = summarize([row])
        self.assertEqual(summary["evaluated_choices"], 0)
        self.assertIsNone(summary["mean_virtual_goal_completion_seconds"])

    def test_model_failure_is_not_replaced_with_calculated_answer(self):
        class Broken(FixedChoiceSelector):
            last_inference = {"response": "previous unrelated response"}

            def select_choice(self, *args, **kwargs):
                raise RuntimeError("model unavailable")

        row = run_trial(self.cases["three_robot_different_distances"], "kev", selector=Broken())
        self.assertEqual(row["status"], "FAILED")
        self.assertNotIn("selected_id", row)
        self.assertNotIn("inference", row)
        self.assertEqual(summarize([row])["outcomes"], {"FAILED": 1})

    def test_unknown_strategy_is_rejected(self):
        scene = TrafficSituation(deepcopy(self.cases["three_robot_different_distances"]["payload"]))
        with self.assertRaisesRegex(ValueError, "선택 방식"):
            scene.decide(strategy="unknown")

    def test_repeat_cli_records_inputs_trials_and_clock_labels(self):
        with TemporaryDirectory() as folder:
            output = Path(folder) / "comparison.json"
            result = main(["--without-kev", "--case", "kev_joint_yield_or_reroute",
                           "--repeats", "2", "--output", str(output)])
            self.assertEqual(result, 0)
            data = json.loads(output.read_text())
            self.assertEqual(data["state"], "COMPLETED")
            case = data["cases"]["kev_joint_yield_or_reroute"]
            self.assertIn("payload", case)
            self.assertEqual(len(case["trials"]), 4)
            self.assertTrue(all(r["actual_metrics"]["clock"] == "VIRTUAL_SIMULATION" for r in case["trials"]))
            self.assertIn("가상 시계", output.with_suffix(".md").read_text())


if __name__ == "__main__":
    unittest.main()
