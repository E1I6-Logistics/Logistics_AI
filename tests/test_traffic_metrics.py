"""시간 기준·정차 로봇·미완료·양보 복귀를 잘못 계산하지 않는지 확인한다."""

import json
from pathlib import Path
import unittest

from backend.app.services.traffic_joint_actions import JointAction, Step, TrafficSituation
from backend.app.services.traffic_metrics import TrafficRunMetrics, estimate_plan
from backend.simulation.traffic_joint_actions import TestSelector as FixedChoiceSelector


class TrafficMetricsTests(unittest.TestCase):
    def setUp(self):
        self.nodes = {"a": (0, 0), "b": (1, 0), "c": (0, 1), "d": (3, 1)}
        self.requests = {
            "r1": dict(current_node="a", node="b", waiting_seconds=10),
            "r2": dict(current_node="c", node="d", waiting_seconds=0),
            "parked": dict(current_node="b", node="b", waiting_seconds=0),
        }
        self.short = Step("r1", "PROCEED", ("a", "b"))
        self.long = Step("r2", "PROCEED", ("c", "d"))

    def estimate(self, steps, requests=None):
        return estimate_plan(
            JointAction("candidate", "TEST", tuple(steps), ()),
            requests or self.requests, self.nodes, speed_mps=0.2,
        )

    def test_order_changes_average_but_not_total_travel_time(self):
        short_first = self.estimate([self.short, self.long])
        long_first = self.estimate([self.long, self.short])
        self.assertEqual(short_first["mean_goal_completion_seconds"], 12.5)
        self.assertEqual(long_first["mean_goal_completion_seconds"], 17.5)
        self.assertEqual(short_first["all_steps_completion_seconds"], 20)
        self.assertEqual(long_first["all_steps_completion_seconds"], 20)
        self.assertEqual(short_first["moving_request_count"], 2)
        self.assertEqual(short_first["total_distance_m"], 4)

    def test_hold_is_unknown_completion_not_zero_time(self):
        estimate = self.estimate([])
        self.assertIsNone(estimate["mean_goal_completion_seconds"])
        recorder = TrafficRunMetrics(self.requests, self.nodes, requested_at=100, clock_kind="TEST")
        recorder.begin_execution(102)
        measured = recorder.report(110, outcome="UNRESOLVED")
        self.assertIsNone(measured["mean_goal_completion_seconds"])
        self.assertEqual(measured["unfinished_robot_ids"], ["r1", "r2"])
        self.assertEqual(measured["max_request_to_first_step_seconds"], 20)

    def test_yielding_static_robot_does_not_lower_average_and_return_is_counted(self):
        requests = {key: value for key, value in self.requests.items() if key != "r2"}
        out = Step("parked", "YIELD", ("b", "d"))
        back = Step("parked", "PROCEED", ("d", "b"))
        estimate = self.estimate([out, self.short, back], requests)
        out_time = (5 ** 0.5) / 0.2
        self.assertEqual(estimate["moving_request_count"], 1)
        self.assertAlmostEqual(estimate["mean_goal_completion_seconds"], out_time + 5)
        self.assertAlmostEqual(estimate["all_steps_completion_seconds"], 2 * out_time + 5)
        self.assertAlmostEqual(estimate["robots"]["parked"]["step_distance_m"], 2 * 5 ** 0.5)

    def test_actual_separates_setup_turn_wait_step_duration_and_prior_wait(self):
        recorder = TrafficRunMetrics(self.requests, self.nodes, requested_at=100, clock_kind="TEST")
        recorder.begin_execution(102, selection_seconds=1)
        recorder.start_step(1, self.short, 102)
        recorder.complete_step(self.short, 108)  # 6초: 거리 예측 5초와 달라도 그대로 기록
        partial = recorder.report(109)
        self.assertIsNone(partial["mean_goal_completion_seconds"])
        self.assertEqual(partial["unfinished_robot_ids"], ["r2"])
        recorder.start_step(2, self.long, 109)
        recorder.complete_step(self.long, 125)
        measured = recorder.report(125, outcome="COMPLETED")
        self.assertEqual(measured["setup_seconds"], 2)
        self.assertEqual(measured["selection_seconds"], 1)
        self.assertEqual(measured["mean_goal_completion_seconds"], 14.5)  # (6 + 23) / 2
        self.assertEqual(measured["mean_request_to_goal_seconds"], 21.5)  # (10 + 8 + 25) / 2
        self.assertEqual(measured["max_turn_wait_seconds"], 7)
        self.assertEqual(measured["steps"][0]["active_seconds"], 6)
        self.assertEqual(measured["robots"]["r2"]["completed_path_distance_m"], 3)
        # 보고용 복사본을 바꿔도 다음 보고가 바뀌지 않는다.
        measured["steps"][0]["active_seconds"] = -1
        self.assertEqual(recorder.report(125)["steps"][0]["active_seconds"], 6)

    def test_interruption_keeps_partial_step_and_unfinished_request(self):
        recorder = TrafficRunMetrics(self.requests, self.nodes, requested_at=100, clock_kind="TEST")
        recorder.begin_execution(101)
        recorder.start_step(1, self.short, 101)
        measured = recorder.report(104, outcome="INTERRUPTED")
        self.assertIsNone(measured["steps"][0]["completed_seconds"])
        self.assertEqual(measured["completed_request_count"], 0)
        self.assertEqual(measured["robots"]["r1"]["completed_path_distance_m"], 0)

    def test_nonpositive_or_nonfinite_prediction_speed_is_rejected(self):
        for speed in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(speed=speed), self.assertRaises(ValueError):
                estimate_plan(JointAction("x", "HOLD", (), ()), self.requests, self.nodes, speed_mps=speed)

    def test_selector_really_receives_estimates_and_preserves_validated_plan(self):
        fixtures = json.loads(Path(__file__).with_name("traffic_situation_cases.json").read_text())
        scene = TrafficSituation({**fixtures["cases"]["three_robot_node"], "graph": fixtures["graph"]})

        class RecordingSelector(FixedChoiceSelector):
            def select_choice(inner, state, candidates, instructions, **kwargs):
                self.assertEqual(set(candidates), set(scene.plans))
                for key, raw in candidates.items():
                    offered = json.loads(raw)
                    self.assertEqual(offered["estimated_metrics"], scene.estimates[key])
                    self.assertEqual(offered["release_rule"], "PREVIOUS_STEP_COMPLETED")
                self.assertIn("exclude inference", instructions)
                self.assertIn("Primary objective: MINIMIZE", instructions)
                return {"choice": "A2"}

        self.assertEqual(scene.decide(RecordingSelector()).joint_action_id, "A2")
        self.assertEqual(scene.preview()["candidate_estimates"], scene.estimates)


if __name__ == "__main__":
    unittest.main()
