"""Pending robots must be recoverable without bypassing KEV or losing their jobs."""

import json
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from backend.app.services.kev_mapf.kev_admission_recovery import (
    admission_snapshot, plan_admission_recovery, apply_admission_recovery,
)
from backend.app.services.kev_mapf.kev_coordinator import FmsMapfCoordinator
from backend.app.services.kev_mapf.kev_graph import Graph
from backend.app.services.kev_mapf.kev_recovery import KevRecovery
from backend.app.services.mock_data import MockFmsStore
from backend.app.services.reservation import ReservationTable


class AdmissionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.graph = json.loads((Path(__file__).resolve().parents[1] / "routes/test.geojson").read_text())
        self.store = MockFmsStore(ReservationTable())
        self.c = FmsMapfCoordinator(self.store)
        self.requests = {}
        for i, path in enumerate([[8, 7, 11, 6], [9, 7, 5], [7, 9, 2]], 1):
            rid = f"robot{i}"
            x, y = Graph(self.graph).nodes[path[0]]
            self.store._robots[rid].update(x=x, y=y, current_node=str(path[0]), occupied_node=str(path[0]))
            order = {"robot_id": rid, "selector": "kev", "model": "kev-08b"}
            req = self.c.stage_preview(order, path[0], path[-1], {"path": list(map(str, path))})
            self.requests[rid] = {"request": req, "result": req["result"], "graph": self.graph}

    def choose(self, rid="robot3", target=11):
        snapshot = admission_snapshot(self.store, self.c)
        with patch(
            "backend.app.services.kev_mapf.kev_recovery.preview_llm_route", side_effect=ValueError("no full detour")
        ), patch(
            "backend.app.services.kev_mapf.kev_recovery.KevSelector.select_choice",
            return_value={"choice": f"move_aside_{rid}_{target}", "confidence": 1.0},
        ) as select:
            proposal = plan_admission_recovery(self.graph, snapshot)
        self.assertIn("move_aside_robot1_10", select.call_args.args[1])
        self.assertIn("move_aside_robot3_11", select.call_args.args[1])
        return snapshot, proposal

    def apply(self, snapshot, proposal, rid="robot3"):
        return apply_admission_recovery(
            self.store, self.c, self.graph, snapshot, proposal,
            robot_id=rid, prepared=self.requests[rid], valid=lambda: True,
        )

    def test_pending_aside_preserves_original_goal_and_stays_unfinished(self):
        snapshot, proposal = self.choose()
        result = self.apply(snapshot, proposal)
        r = self.store._robots["robot3"]
        self.assertEqual(r["goal_node"], "2")
        self.assertEqual(r["kev_maneuver"], {"target_node": "11", "phase": "MOVING_ASIDE"})
        self.assertEqual(self.store._traffic._required_paths["robot3"], ("7", "11"))
        self.assertNotIn("robot3", self.c._pending_requests)
        self.assertEqual(str(self.c._route_requests["robot3"]["node"]), "2")
        self.assertEqual(result["mapf"]["analysis_source"], "KEV_PENDING_RECOVERY")
        self.store._traffic._finish_route(r)
        self.assertEqual(r["status"], "WAITING")
        self.assertEqual(r["goal_node"], "2")

    def test_kev_can_choose_a_different_pending_robot(self):
        snapshot, proposal = self.choose("robot1", 10)
        self.assertEqual(proposal[0], "robot1")
        result = self.apply(snapshot, proposal, "robot1")
        self.assertIsNotNone(result)
        self.assertEqual(self.store._robots["robot1"]["goal_node"], "6")

    def test_other_request_task_does_not_dispatch_selected_robot(self):
        snapshot, proposal = self.choose()
        before = deepcopy(self.store._robots)
        self.assertIsNone(self.apply(snapshot, proposal, "robot1"))
        self.assertEqual(before, self.store._robots)

    def test_stale_fleet_or_replaced_pending_does_not_start(self):
        snapshot, proposal = self.choose()
        self.store._robots["robot1"]["occupied_node"] = "10"
        self.assertIsNone(self.apply(snapshot, proposal))
        self.assertIsNone(self.store._robots["robot3"]["goal_node"])

    def test_identical_replacement_request_invalidates_previous_inference(self):
        snapshot, proposal = self.choose()
        req = self.requests["robot3"]["request"]
        newer = self.c.stage_preview(req["order"], req["current_node"], req["node"], req["result"])
        self.assertNotEqual(req["request_id"], newer["request_id"])
        self.assertIsNone(self.apply(snapshot, proposal))
        self.assertIsNone(self.store._robots["robot3"]["goal_node"])
        self.assertEqual(self.c._pending_requests["robot3"], newer)

    def test_unchanged_pending_path_is_a_valid_kev_proceed_option(self):
        # Remove physical blockers while retaining the pending request's path.
        self.store._robots["robot1"].update(occupied_node="0", current_node="0")
        self.store._robots["robot2"].update(occupied_node="1", current_node="1")
        for rid in ["robot1", "robot2"]:
            self.c.discard_pending(rid)
        snapshot = admission_snapshot(self.store, self.c)
        with patch(
            "backend.app.services.kev_mapf.kev_recovery.preview_llm_route", return_value={"path": ["7", "9", "2"]}
        ), patch(
            "backend.app.services.kev_mapf.kev_recovery.KevSelector.select_choice",
            return_value={"choice": "proceed_robot3"},
        ):
            proposal = plan_admission_recovery(self.graph, snapshot)
        self.assertEqual(proposal[2]["action"], "PROCEED")


if __name__ == "__main__":
    unittest.main()
