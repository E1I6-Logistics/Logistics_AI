"""[테스트 모듈: test_fms_mapf_coordinator.py - FMS MAPF Coordinator 및 분리된 kev_ 모듈 단위 테스트]"""

import unittest
from unittest.mock import patch

from backend.app.services.fms_mapf_coordinator import (
    FmsMapfCoordinator,
    _is_valid_suffix,
    is_fms_reviewable_advisory,
)
from backend.app.services.kev_mapf import (
    KevMAPF,
    parse_robot_state,
    plan_mapf_from_route_results,
    would_cycle,
)
from tests.mock.kev_mock import MockKEV, demo_data


class KevCoordinatorAndMockTests(unittest.TestCase):
    def test_demo_data_and_mock_kev_resolve(self):
        graph, jobs = demo_data()
        mock = MockKEV()
        robots = parse_robot_state(jobs, graph, mock)
        res = KevMAPF(graph, robots, mock, jobs).resolve()
        self.assertEqual(res["status"], "PROPOSED")
        self.assertFalse(res["fms_execution_approved"])
        self.assertEqual(len(res["decisions"]), 1)
        self.assertEqual(res["decisions"][0]["action"], "WAIT")

    def test_suffix_validation_helper(self):
        self.assertTrue(_is_valid_suffix(["1", "2", "3", "4"], ["3", "4"]))
        self.assertTrue(_is_valid_suffix(["1", "2", "3"], ["1", "2", "3"]))
        self.assertFalse(_is_valid_suffix(["1", "2", "3"], ["1", "2"]))
        self.assertFalse(_is_valid_suffix(["1", "2"], []))
        self.assertFalse(_is_valid_suffix(["1", "2"], None))

    def test_would_cycle_detection(self):
        priorities = {("a", "b"), ("b", "c")}
        self.assertTrue(would_cycle(priorities, "c", "a"))
        self.assertFalse(would_cycle(priorities, "a", "c"))

    def test_is_fms_reviewable_advisory(self):
        self.assertTrue(is_fms_reviewable_advisory({"status": "PROPOSED"}))
        self.assertFalse(is_fms_reviewable_advisory({"status": "BLOCKED"}))
        review_ok = {
            "status": "REVIEW_REQUIRED",
            "decisions": [{"action": "WAIT", "wait_node_verified": True, "wait_node": 2}],
            "warnings": ["amr_2: WAIT node 2 overlaps routes of amr_1; FMS traffic reservation verification required"],
        }
        self.assertTrue(is_fms_reviewable_advisory(review_ok))

    def test_coordinator_stage_and_register(self):
        coord = FmsMapfCoordinator()
        order = {"robot_id": "r1", "selector": "kev", "model": "kev-08b"}
        coord.stage_preview(order, "1", "4", {"path": ["1", "2", "4"]})
        self.assertEqual(len(coord.pending_snapshot()), 1)

        coord.register_preview(order, "1", "4", {"path": ["1", "2", "4"]})
        coord.discard_pending("r1")
        self.assertEqual(len(coord.pending_snapshot()), 0)
        self.assertEqual(len(coord.snapshot()), 1)

        coord.remove("r1")
        self.assertEqual(len(coord.snapshot()), 0)

    def test_coordinator_mixed_simulation_advisory_flow(self):
        coord = FmsMapfCoordinator()
        graph, _ = demo_data()

        order1 = {"robot_id": "r1", "selector": "kev", "model": "kev-08b"}
        order2 = {"robot_id": "r2", "selector": "kev", "model": "kev-08b"}

        coord.register_preview(order1, 1, 4, {"path": [1, 2, 3, 4]})
        coord.stage_preview(order2, 5, 7, {"path": [5, 6, 3, 7]})

        fake_snapshots = [
            {
                "robot_id": "r1",
                "status": "NAVIGATING",
                "connection_state": "ONLINE",
                "stop_requested": False,
                "occupied_node": "2",
                "occupied_edge": None,
                "goal_node": "4",
                "navigation_id": "nav1",
                "route": {
                    "node_ids": ["1", "2", "3", "4"],
                    "segment_index": 1,
                    "phase": "MOVING",
                    "departure_at": 0.0,
                    "segment_departures": [],
                },
            },
            {
                "robot_id": "r2",
                "status": "IDLE",
                "connection_state": "ONLINE",
                "stop_requested": False,
                "occupied_node": "5",
                "occupied_edge": None,
                "goal_node": None,
                "navigation_id": None,
                "route": None,
            },
        ]

        fake_mock_data = unittest.mock.MagicMock()
        fake_mock_data.mock_fms.robot_snapshots.return_value = fake_snapshots
        with patch.dict("sys.modules", {"backend.app.services.mock_data": fake_mock_data}):
            advisory = coord.evaluate_mixed_simulation_advisory(
                graph.data,
                planner=MockKEV(),
                config={"wait_allowed_nodes": [5, 6], "max_reroutes": 0},
            )
            self.assertEqual(advisory["status"], "PROPOSED")
            self.assertEqual(advisory["active_robot_ids"], ["r1"])
            self.assertEqual(advisory["pending_robot_ids"], ["r2"])

            policy = coord.classify_mixed_pending_advisory(advisory)
            self.assertEqual(policy["status"], "REVIEW_ONLY")
            self.assertIn("r2", policy["deferred"])
            self.assertEqual(policy["fms_check_candidates"], [])


if __name__ == "__main__":
    unittest.main()
