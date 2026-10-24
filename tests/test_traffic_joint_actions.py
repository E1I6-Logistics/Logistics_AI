"""실제 TrafficManager/MockFmsStore로 교통조정안의 선택·이동을 검증한다."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import unittest

from backend.app.services.reservation import Reservation, node_key
from backend.app.services.traffic_joint_actions import TrafficSituation
from backend.simulation.traffic_joint_actions import JointActionSimulation, TestSelector


def fixtures():
    data = json.loads(
        Path(__file__).with_name("traffic_situation_cases.json").read_text(encoding="utf-8")
    )
    return {name: {**case, "graph": deepcopy(data["graph"])} for name, case in data["cases"].items()}


class JointActionTests(unittest.TestCase):
    def setUp(self):
        self.cases = fixtures()

    def run_case(self, name, choice=None):
        scene = TrafficSituation(self.cases[name])
        simulator = JointActionSimulation(scene)
        decision = scene.decide(TestSelector(choice))
        return scene, simulator, simulator.execute(decision)

    def test_three_robot_joint_choices_change_actual_departure_order(self):
        for choice, first in [("A1", "robot1"), ("A2", "robot2"), ("A3", "robot3")]:
            with self.subTest(choice=choice):
                scene, simulator, result = self.run_case("three_robot_node", choice)
                self.assertEqual(set(scene.plans), {"A1", "A2", "A3"})
                self.assertEqual(result["status"], "COMPLETED")
                self.assertEqual(result["events"][0]["robot_id"], first)
                self.assertEqual(result["final_nodes"], {"robot1": "3", "robot2": "4", "robot3": "5"})
                self.assertGreater(result["capacity_checks"], 0)
                self.assertEqual(result["capacity_violations"], 0)
                self.assertFalse(simulator.store._traffic._reservations.snapshot())

    def test_wait_does_not_release_the_occupied_node(self):
        self.cases["three_robot_node"]["config"]["allow_proactive_hold"] = True
        _, simulator, result = self.run_case("three_robot_node", "HOLD_ALL")
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(result["steps_completed"], 0)
        self.assertEqual(result["final_nodes"], {"robot1": "0", "robot2": "1", "robot3": "2"})
        self.assertTrue(all(r["route"] is None for r in simulator.store._robots.values()))

    def test_reroute_avoids_occupied_node_and_preserves_other_robots(self):
        scene, _, result = self.run_case("reroute_occupied_node", "B_REROUTE")
        self.assertNotIn("A1", scene.plans)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["events"][0]["path"], ["0", "6", "3"])
        self.assertEqual(result["final_nodes"], {"robot1": "3", "robot2": "13", "robot3": "2"})

    def test_yield_finishes_before_proceed_and_restores_original_goal(self):
        _, _, result = self.run_case("yield_occupied_node", "B_YIELD_robot2")
        starts = [event for event in result["events"] if event["event"] == "STEP_STARTED"]
        self.assertEqual([(e["robot_id"], e["action"]) for e in starts], [
            ("robot2", "YIELD"), ("robot1", "PROCEED"), ("robot2", "PROCEED"),
        ])
        self.assertEqual(result["events"][1]["event"], "STEP_COMPLETED")
        self.assertEqual(result["events"][1]["node"], "8")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["final_nodes"]["robot2"], "13")

    def test_no_feasible_movement_is_unresolved_without_model_call(self):
        scene = TrafficSituation(self.cases["all_wait"])
        selector = TestSelector()
        decision = scene.decide(selector)
        self.assertEqual(selector.calls, 0)
        self.assertEqual(decision.selection_source, "SINGLE_OPTION")
        simulator = JointActionSimulation(scene)
        before = deepcopy(simulator.store._robots)
        self.assertEqual(simulator.execute(decision)["status"], "UNRESOLVED")
        self.assertEqual(simulator.store._robots, before)

    def test_blocked_edge_applies_to_both_directions(self):
        scene, _, result = self.run_case("reroute_blocked_edge", "B_REROUTE")
        self.assertEqual(scene.rejected["A1"], "BLOCKED_EDGE")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["capacity_violations"], 0)

    def test_duplicate_occupancy_and_inconsistent_input_rejected(self):
        for change in ["duplicate", "occupancy"]:
            with self.subTest(change=change):
                payload = deepcopy(self.cases["three_robot_node"])
                if change == "duplicate":
                    request = payload["route_requests"][1]
                    request["current_node"] = "0"
                    request["result"]["path"] = ["0", "13", "4"]
                else:
                    payload["config"]["zone_occupancy"]["node:13"] = "robot3"
                with self.assertRaises(ValueError):
                    TrafficSituation(payload)

    def test_invalid_paths_and_unsafe_wait_node_rejected(self):
        changes = [
            lambda p: p["route_requests"][0]["result"].update(path=["0", "3"]),
            lambda p: p["config"].update(wait_allowed_nodes=["0", "1"]),
            lambda p: p["config"].update(reroute_paths={"robot1": ["0", "6", "4"]}),
            lambda p: p["config"].update(yield_paths={"robot2": {"out": ["1", "13", "4"], "resume": ["4"]}}),
        ]
        for change in changes:
            with self.subTest(change=change):
                payload = deepcopy(self.cases["three_robot_node"])
                change(payload)
                with self.assertRaises(ValueError):
                    TrafficSituation(payload)

    def test_moving_or_reserved_or_real_input_is_not_silently_supported(self):
        for key in ["moving", "reserved", "real"]:
            with self.subTest(key=key):
                payload = deepcopy(self.cases["three_robot_node"])
                if key == "moving":
                    payload["route_requests"][0]["status"] = "NAVIGATING"
                elif key == "reserved":
                    payload["config"]["reservations"] = [{"robot_id": "robot1"}]
                else:
                    payload["mode"] = "real"
                with self.assertRaises(ValueError):
                    TrafficSituation(payload)

    def test_invalid_model_choice_never_reaches_execution(self):
        scene = TrafficSituation(self.cases["three_robot_node"])
        simulator = JointActionSimulation(scene)
        before = deepcopy(simulator.store._robots)
        with self.assertRaisesRegex(ValueError, "알 수 없는"):
            scene.decide(TestSelector("NOT_A_PLAN"))
        self.assertEqual(simulator.store._robots, before)

    def test_state_change_during_inference_rejected(self):
        payload = self.cases["three_robot_node"]
        scene = TrafficSituation(payload)

        class ChangingSelector(TestSelector):
            def select_choice(self, *args, **kwargs):
                payload["route_requests"][0]["current_node"] = "13"
                return {"choice": "A1"}

        with self.assertRaisesRegex(ValueError, "STALE_INPUT"):
            scene.decide(ChangingSelector())

    def test_different_snapshot_or_late_reservation_rejected_without_mutation(self):
        scene = TrafficSituation(self.cases["three_robot_node"])
        decision = scene.decide(TestSelector())
        simulator = JointActionSimulation(scene)
        with self.assertRaisesRegex(ValueError, "STALE_DECISION"):
            simulator.execute(replace(decision, snapshot_hash="other"))
        before = deepcopy(simulator.store._robots)
        simulator.store._traffic._reservations.try_commit(
            "robot1", "already-approved",
            [Reservation(node_key("0"), "robot1", "already-approved", 0, 0, 10)],
        )
        with self.assertRaisesRegex(ValueError, "STALE_SIMULATION"):
            simulator.execute(decision)
        self.assertEqual(simulator.store._robots, before)
        self.assertEqual(len(simulator.store._traffic._reservations.snapshot()), 1)

    def test_same_decision_cannot_execute_twice(self):
        self.cases["three_robot_node"]["config"]["allow_proactive_hold"] = True
        scene = TrafficSituation(self.cases["three_robot_node"])
        simulator = JointActionSimulation(scene)
        decision = scene.decide(TestSelector("HOLD_ALL"))
        simulator.execute(decision)
        with self.assertRaisesRegex(ValueError, "STALE_SIMULATION"):
            simulator.execute(decision)

    def test_hold_is_removed_before_model_call_when_progress_is_feasible(self):
        scene = TrafficSituation(self.cases["three_robot_node"])
        captured = {}

        class RecordingSelector(TestSelector):
            def select_choice(self, state, candidates, *args, **kwargs):
                captured.update(candidates)
                return {"choice": "A2"}

        decision = scene.decide(RecordingSelector())
        self.assertEqual(set(captured), {"A1", "A2", "A3"})
        self.assertEqual(decision.joint_action_id, "A2")
        self.assertFalse(scene.preview()["policy"]["allow_proactive_hold"])

    def test_unoffered_hold_response_is_rejected_not_silently_replaced(self):
        scene = TrafficSituation(self.cases["three_robot_node"])
        simulator = JointActionSimulation(scene)
        before = deepcopy(simulator.store._robots)
        with self.assertRaisesRegex(ValueError, "알 수 없는"):
            scene.decide(TestSelector("HOLD_ALL"))
        self.assertEqual(simulator.store._robots, before)


if __name__ == "__main__":
    unittest.main()
