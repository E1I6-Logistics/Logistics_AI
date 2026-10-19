"""[테스트 모듈: test_fms_mapf_coordinator.py - FMS MAPF Coordinator, Dispatcher 및 REROUTE 파이프라인 단위 테스트]"""

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
    review_prepared_mapf_candidate,
    would_cycle,
)
from tests.mock.kev_mock import MockKEV, demo_data


class ReroutePlanner(MockKEV):
    """action_reroute를 선택하고 우회 경로를 반환하는 테스트 전용 Planner."""

    def __init__(self, rerouted_path: list[int]):
        self.rerouted_path = rerouted_path

    def pick(self, state, candidates, instructions, question_id):
        if question_id.startswith("action_") and "action_reroute" in candidates:
            return "action_reroute"
        return super().pick(state, candidates, instructions, question_id)

    def route(self, graph_data, start, goal, *, selector="kev", model=None, traffic=None):
        return list(self.rerouted_path)


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
            "warnings": [
                "amr_2: WAIT node 2 overlaps routes of amr_1; FMS traffic reservation verification required"
            ],
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

    def test_reroute_is_accepted_by_coordinator_and_dispatcher(self):
        """REROUTE 성공 시 Coordinator가 차단하지 않고 출발 후보로 분류하며, Dispatcher가 우회 경로를 채택하는지 검증."""
        from tests.test_kev_mapf_integration import graph_data

        g_data = graph_data()
        coord = FmsMapfCoordinator()
        order_a = {"robot_id": "a", "selector": "kev", "model": "kev-08b", "mode": "simulation"}
        order_b = {"robot_id": "b", "selector": "kev", "model": "kev-08b", "mode": "simulation"}

        coord.register_preview(order_a, "1", "4", {"path": ["1", "2", "3", "4"]})
        req_b = coord.stage_preview(order_b, "5", "7", {"path": ["5", "3", "7"]})
        prepared_b = {
            "graph": g_data,
            "request": req_b,
            "result": {"path": ["5", "3", "7"]},
            "required_path": ["5", "3", "7"],
        }

        fake_snapshots = [
            {
                "robot_id": "a",
                "status": "NAVIGATING",
                "connection_state": "ONLINE",
                "stop_requested": False,
                "occupied_node": "1",
                "occupied_edge": None,
                "goal_node": "4",
                "navigation_id": "nav_a",
                "route": {
                    "node_ids": ["1", "2", "3", "4"],
                    "segment_index": 0,
                    "phase": "MOVING",
                    "departure_at": 0.0,
                    "segment_departures": [],
                },
            },
            {
                "robot_id": "b",
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
        fake_mock_data.mock_fms.robot_snapshot.return_value = fake_snapshots[1]

        with (
            patch.dict("sys.modules", {"backend.app.services.mock_data": fake_mock_data}),
            patch("backend.app.services.kev_mapf.kev_dispatcher.fms_mapf_coordinator", coord),
            patch("backend.app.services.kev_mapf.kev_dispatcher.load_route_graph", return_value=g_data),
            patch("backend.app.services.kev_mapf.kev_dispatcher.mode_manager", unittest.mock.MagicMock(mode="simulation")),
        ):
            advisory = coord.evaluate_mixed_simulation_advisory(
                g_data,
                planner=ReroutePlanner([5, 6, 7]),
                config={"wait_allowed_nodes": [], "max_reroutes": 3, "priority_mode": "choice"},
            )
            self.assertEqual(advisory["status"], "PROPOSED")
            self.assertEqual(advisory["required_paths"]["b"], ["5", "6", "7"])

            policy = coord.classify_mixed_pending_advisory(advisory)
            self.assertEqual(policy["status"], "REVIEW_ONLY")
            self.assertIn("b", policy["fms_check_candidates"])
            self.assertEqual(policy["rerouted"]["b"], ["5", "6", "7"])

            reviewed = review_prepared_mapf_candidate(
                order_b, "7", "5", prepared_b, advisory
            )
            self.assertEqual(reviewed["status"], "REVIEW_ONLY")
            self.assertTrue(reviewed["rerouted"])
            self.assertEqual(reviewed["required_path"], ["5", "6", "7"])


if __name__ == "__main__":
    unittest.main()
