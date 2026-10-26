"""Parallel admission, in-flight route stability and zone-local KEV WAIT."""

from copy import deepcopy
import math
import os
import unittest
from unittest.mock import patch

from backend.app.services.kev_mapf.kev_adapter import plan_mapf_from_route_results
from backend.app.services.kev_mapf.kev_coordinator import FmsMapfCoordinator
from backend.app.services.kev_mapf.kev_parallel_admission import fleet_snapshot
from backend.app.services.mock_data import MockFmsStore
from backend.app.services.reservation import ReservationTable, node_key
from tests.mock.kev_mock import MockKEV


def network(shared=False):
    points = {0: (0, 0), 1: (0, 2), 2: (0, 4), 3: (2, 0), 4: (2, 2), 5: (2, 4)}
    edges = [(0, 3), (1, 4), (2, 5)]
    if shared:
        points.update({0: (0, 0), 1: (1, -1), 3: (1, 0), 4: (1, 1), 6: (4, 0)})
        edges = [(0, 3), (3, 6), (1, 3), (3, 4), (2, 5)]
    return {"type": "FeatureCollection", "features": [
        {"geometry": {"type": "Point", "coordinates": list(xy)}, "properties": {"id": str(n)}}
        for n, xy in points.items()
    ] + [
        {"geometry": {"type": "MultiLineString"}, "properties": {
            "id": f"e{i}", "startid": str(a), "endid": str(b), "weight": math.dist(points[a], points[b]),
        }} for i, (a, b) in enumerate(edges)
    ]}


class ParallelAdmissionTests(unittest.TestCase):
    def setup_fleet(self, shared=False):
        self.graph = network(shared)
        self.store = MockFmsStore(ReservationTable())
        self.c = FmsMapfCoordinator(self.store)
        points = {f["properties"]["id"]: f["geometry"]["coordinates"] for f in self.graph["features"] if f["geometry"]["type"] == "Point"}
        for i in range(3):
            self.store._robots[f"robot{i+1}"].update(x=points[str(i)][0], y=points[str(i)][1])

    def stage(self, rid, path):
        return self.c.stage_preview({"robot_id": rid, "selector": "kev", "model": "kev-08b"},
                                    path[0], path[-1], {"path": list(map(str, path))},
                                    execution_epoch=self.store.robot_snapshot(rid, "simulation").get("command_epoch"))

    def plan(self, planner=None):
        snapshot = fleet_snapshot(self.store.mapf, self.c)
        advisory = plan_mapf_from_route_results(self.graph, snapshot["requests"], planner=planner or MockKEV(), config={
            "wait_allowed_nodes": [r["current_node"] for r in snapshot["pending"].values()],
        })
        return snapshot, advisory

    def test_fms_priority_is_score_only_and_rejects_choice_setting(self):
        from backend.app.services.kev_mapf.kev_dispatcher import mapf_priority_mode
        with patch.dict(os.environ):
            os.environ.pop("FMS_MAPF_PRIORITY_MODE", None)
            self.assertEqual(mapf_priority_mode(), "score")
            os.environ["FMS_MAPF_PRIORITY_MODE"] = "choice"
            with self.assertRaisesRegex(ValueError, "only score"):
                mapf_priority_mode()

    def test_engine_rejects_removed_choice_priority_mode_before_model_call(self):
        from unittest.mock import Mock
        self.setup_fleet()
        self.stage("robot1", [0, 3])
        live = Mock()
        with self.assertRaisesRegex(ValueError, "only score"):
            plan_mapf_from_route_results(self.graph, self.c.pending_snapshot(), planner=live,
                                         config={"priority_mode": "choice"})
        live.score_many.assert_not_called()
        live.pick.assert_not_called()

    def test_three_disjoint_routes_are_admitted_in_one_atomic_cohort(self):
        self.setup_fleet()
        for i in range(3):
            self.stage(f"robot{i+1}", [i, i+3])
        snapshot, advisory = self.plan()
        self.assertTrue(self.c.admission.apply(self.store.mapf, self.graph, snapshot, advisory))
        robots = list(self.store._robots.values())
        self.assertTrue(all(r["goal_node"] is not None and r["route"]["departure_at"] is not None for r in robots))
        self.assertEqual(len({o["mapf"]["plan_id"] for o in self.c.admission.outcome_snapshot().values()}), 1)
        self.assertEqual(len(self.c.snapshot()), 3)
        self.assertFalse(self.c.pending_snapshot())

    def test_confirmed_stop_before_new_order_does_not_disable_parallel_admission(self):
        self.setup_fleet()
        for i in range(3):
            rid = f"robot{i+1}"
            self.store.stop_robot(rid)  # Same stop performed after pickup/workstation arrival.
            self.stage(rid, [i, i+3])
        snapshot, advisory = self.plan()
        self.assertTrue(self.c.admission.apply(self.store.mapf, self.graph, snapshot, advisory))
        self.assertEqual(len(self.c.admission.outcome_snapshot()), 3)
        self.assertTrue(all(r["route"] is not None for r in self.store.robot_snapshots("simulation")))

    def test_repeated_stop_cancels_prepared_order_even_if_flag_was_already_true(self):
        self.setup_fleet()
        self.store.stop_robot("robot1")
        self.stage("robot1", [0, 3])
        snapshot, advisory = self.plan()
        self.store.stop_robot("robot1")
        self.assertIsNone(fleet_snapshot(self.store.mapf, self.c))
        self.assertFalse(self.c.admission.apply(self.store.mapf, self.graph, snapshot, advisory))
        self.assertIsNone(self.store.robot_snapshot("robot1", "simulation")["goal_node"])

    def stage_three_way_junction(self):
        self.setup_fleet()
        self.graph["features"] = [f for f in self.graph["features"] if f["geometry"]["type"] == "Point"]
        self.graph["features"].append({"geometry": {"type": "Point", "coordinates": [1, 2]}, "properties": {"id": "7"}})
        points = {f["properties"]["id"]: f["geometry"]["coordinates"] for f in self.graph["features"]}
        for i, (a, b) in enumerate([(0, 7), (1, 7), (2, 7), (7, 3), (7, 4), (7, 5)]):
            self.graph["features"].append({"geometry": {"type": "MultiLineString"}, "properties": {
                "id": f"e{i}", "startid": str(a), "endid": str(b), "weight": math.dist(points[str(a)], points[str(b)]),
            }})
        for i in range(3):
            self.stage(f"robot{i+1}", [i, 7, i+3])

    def test_three_routes_competing_for_one_junction_get_kev_zone_dependencies(self):
        self.stage_three_way_junction()
        snapshot, advisory = self.plan()
        self.assertTrue(any(set(d["shared_resource_details"]["robots"]) == {"robot1", "robot2", "robot3"}
                            for d in advisory["decisions"]))
        self.assertTrue(self.c.admission.apply(self.store.mapf, self.graph, snapshot, advisory))
        self.assertEqual(len(self.c.snapshot()), 3)
        self.assertFalse(self.store._robots["robot1"].get("mapf_wait_for"))
        self.assertTrue(self.store._robots["robot2"]["mapf_wait_for"])
        self.assertEqual({d["robot_id"] for d in self.store._robots["robot3"]["mapf_wait_for"]}, {"robot1", "robot2"})

    def test_three_way_scores_are_batched_once_and_reused_without_priority_choices(self):
        class ScoredKEV(MockKEV):
            def __init__(self):
                self.batches, self.questions = [], []
                self.calls = 0

            def score_many(self, state, robot_ids):
                self.batches.append(list(robot_ids))
                self.calls += 1
                return {"robot1": .6, "robot2": .2, "robot3": .9}

            def pick(self, state, candidates, instructions, question_id):
                self.questions.append(question_id)
                return super().pick(state, candidates, instructions, question_id)

        self.stage_three_way_junction()
        live = ScoredKEV()
        snapshot, advisory = self.plan(live)
        self.assertEqual(live.batches, [["robot1", "robot2", "robot3"]])
        self.assertFalse(any(q.startswith("priority_") for q in live.questions))
        self.assertEqual(advisory["global_priority_relations"], [["robot1", "robot2"], ["robot3", "robot1"], ["robot3", "robot2"]])
        self.assertEqual(advisory["metrics"]["kev_score_calls"], 1)
        self.assertEqual(advisory["metrics"]["kev_api_calls"], advisory["metrics"]["kev_choice_calls"]+1)
        self.assertTrue(self.c.admission.apply(self.store.mapf, self.graph, snapshot, advisory))
        for outcome in self.c.admission.outcome_snapshot().values():
            self.assertEqual(outcome["mapf"]["priority_mode"], "score")
            self.assertEqual(outcome["mapf"]["priority_scores"], {"robot1": .6, "robot2": .2, "robot3": .9})
        self.assertFalse(self.store._robots["robot3"].get("mapf_wait_for"))
        self.assertEqual({d["robot_id"] for d in self.store._robots["robot2"]["mapf_wait_for"]}, {"robot1", "robot3"})

    def test_equal_scores_keep_the_existing_robot_id_tie_rule(self):
        class TiedKEV(MockKEV):
            def score_many(self, state, robot_ids):
                self.calls += 1
                return {rid: .5 for rid in robot_ids}

        self.stage_three_way_junction()
        _, advisory = self.plan(TiedKEV())
        self.assertEqual(advisory["global_priority_relations"], [["robot1", "robot2"], ["robot1", "robot3"], ["robot2", "robot3"]])
        self.assertEqual(advisory["metrics"]["kev_score_calls"], 1)

    def test_wait_registers_low_request_and_releases_before_high_goal(self):
        self.setup_fleet(shared=True)
        self.stage("robot1", [0, 3, 6])
        self.stage("robot2", [1, 3, 4])
        snapshot, advisory = self.plan()
        self.assertTrue(self.c.admission.apply(self.store.mapf, self.graph, snapshot, advisory))
        low, high = self.store._robots["robot2"], self.store._robots["robot1"]
        self.assertEqual(low["goal_node"], "4")
        self.assertTrue(self.store._traffic._mapf_zone_waiting(low))
        # High has cleared shared node 3 and is still travelling to distant 6.
        high.update(occupied_node=None, occupied_edge="e1", x=1.5, y=0)
        high["route"]["segment_index"] = 1
        self.assertEqual(high["goal_node"], "6")
        self.assertFalse(self.store._traffic._mapf_zone_waiting(low))

    def test_wait_does_not_block_nonshared_upstream_segment(self):
        self.setup_fleet(shared=True)
        traffic = self.store._traffic
        high, low = self.store._robots["robot1"], self.store._robots["robot2"]
        high.update(goal_node="6", navigation_id="high", route={"node_ids": ["0", "3", "6"], "segment_index": 0})
        low["mapf_wait_for"] = [{"robot_id": "robot1", "navigation_id": "high", "resources": [list(node_key(3))]}]
        traffic._required_paths["robot2"] = ("1", "4", "3", "5")
        self.assertFalse(traffic._mapf_zone_waiting(low))
        low["route"] = {"node_ids": ["1", "4", "3", "5"], "segment_index": 1}
        self.assertTrue(traffic._mapf_zone_waiting(low))

    def test_group_wait_releases_each_resource_as_priority_robot_clears_it(self):
        self.setup_fleet(shared=True)
        traffic = self.store._traffic
        high, low = self.store._robots["robot1"], self.store._robots["robot2"]
        high.update(goal_node="6", navigation_id="high", occupied_node=None, occupied_edge="e0",
                    route={"node_ids": ["0", "3", "6"], "segment_index": 0})
        low["mapf_wait_for"] = [{"robot_id": "robot1", "navigation_id": "high", "resources": [
            ["node", "0"], ["node", "3"], ["edge", "0", "3"],
        ]}]
        traffic._required_paths["robot2"] = ("1", "0", "3", "4")
        # The priority robot still uses the group, but has vacated node 0.
        self.assertFalse(traffic._mapf_zone_waiting(low))
        low["route"] = {"node_ids": ["1", "0", "3", "4"], "segment_index": 1}
        self.assertTrue(traffic._mapf_zone_waiting(low))

    def test_mid_edge_active_is_modeled_and_normal_progress_is_not_stale(self):
        self.setup_fleet(shared=True)
        self.stage("robot1", [0, 3, 6])
        snapshot, advisory = self.plan()
        self.assertTrue(self.c.admission.apply(self.store.mapf, self.graph, snapshot, advisory))
        self.stage("robot2", [1, 3, 4])
        snapshot, advisory = self.plan()
        high = self.store._robots["robot1"]
        high.update(occupied_node=None, occupied_edge="e0", x=.5)
        newer = fleet_snapshot(self.store.mapf, self.c)
        self.assertIsNotNone(newer)
        self.assertEqual(snapshot["identity"], newer["identity"])
        modeled = next(r for r in newer["requests"] if r["order"]["robot_id"] == "robot1")
        self.assertTrue(modeled["execution_committed"])
        self.assertEqual(modeled["occupied_edge"], "e0")
        self.assertTrue(self.c.admission.apply(self.store.mapf, self.graph, snapshot, advisory))

    def test_replaced_command_or_cancelled_member_rejects_entire_trial(self):
        self.setup_fleet()
        self.stage("robot1", [0, 3])
        self.stage("robot2", [1, 4])
        snapshot, advisory = self.plan()
        req = snapshot["pending"]["robot2"]
        self.stage("robot2", [1, 4])
        before = deepcopy(self.store._robots)
        self.assertFalse(self.c.admission.apply(self.store.mapf, self.graph, snapshot, advisory))
        self.assertEqual(self.store._robots, before)
        snapshot, advisory = self.plan()
        self.c.admission.register(snapshot["pending"]["robot2"]["request_id"], lambda: False)
        self.assertFalse(self.c.admission.apply(self.store.mapf, self.graph, snapshot, advisory))
        self.assertEqual(self.store._robots, before)

    def test_fleet_reservation_commit_uses_compare_and_replace(self):
        table = ReservationTable()
        self.assertFalse(table.compare_and_replace(("stale",), ()))
        self.assertEqual(table.snapshot(), ())

    def test_failed_cohort_commit_preserves_fleet_and_registry(self):
        from unittest.mock import patch
        self.setup_fleet()
        self.stage("robot1", [0, 3])
        self.stage("robot2", [1, 4])
        snapshot, advisory = self.plan()
        before = self.store.mapf.snapshot(), self.c.requests_by_robot()
        with patch.object(self.store._traffic._reservations, "compare_and_replace", return_value=False):
            self.assertFalse(self.c.admission.apply(self.store.mapf, self.graph, snapshot, advisory))
        self.assertEqual(before, (self.store.mapf.snapshot(), self.c.requests_by_robot()))
        self.assertFalse(self.c.admission.outcome_snapshot())

    def test_snapshots_cannot_mutate_owned_execution_or_request_state(self):
        self.setup_fleet()
        self.stage("robot1", [0, 3])
        before = self.store.mapf.snapshot(), self.c.requests_by_robot()
        robots, paths, protected = self.store.mapf.snapshot()
        robots["robot1"]["occupied_node"] = "999"
        paths["robot1"] = ("999",)
        protected["robot1"] = {("node", "999")}
        _, pending = self.c.requests_by_robot()
        pending["robot1"]["result"]["path"].append("999")
        self.assertEqual(before, (self.store.mapf.snapshot(), self.c.requests_by_robot()))

    def test_admission_needs_only_public_execution_operations(self):
        self.setup_fleet()
        self.stage("robot1", [0, 3])
        self.stage("robot2", [1, 4])
        execution = self.store.mapf

        class PublicExecution:
            mode = "simulation"
            transaction = execution.transaction
            snapshot = execution.snapshot
            commit_routes = execution.commit_routes

        boundary = PublicExecution()
        snapshot = fleet_snapshot(boundary, self.c)
        advisory = plan_mapf_from_route_results(self.graph, snapshot["requests"], planner=MockKEV())
        self.assertTrue(self.c.admission.apply(boundary, self.graph, snapshot, advisory))
        self.assertEqual(len(self.c.admission.outcome_snapshot()), 2)

    def test_overlap_metric_does_not_count_serial_handover_as_parallel(self):
        from tools.verify_kev_mapf_fms import concurrency
        samples = [
            {"elapsed": 0, "robots": [{"robot_id": "a", "status": "NAVIGATING", "x": 0, "y": 0},
                                      {"robot_id": "b", "status": "IDLE", "x": 0, "y": 2}]},
            {"elapsed": 1, "robots": [{"robot_id": "a", "status": "IDLE", "x": 1, "y": 0},
                                      {"robot_id": "b", "status": "NAVIGATING", "x": 1, "y": 2}]},
        ]
        self.assertEqual(concurrency(samples)["max_simultaneously_moving"], 0)
        for sample in samples:
            for r in sample["robots"]:
                r["status"] = "NAVIGATING"
        self.assertEqual(concurrency(samples), {"max_simultaneously_moving": 2, "overlapping_motion_seconds": 1.0,
                                              "three_robot_motion_seconds": 0.0})
        for sample in samples:
            sample["robots"].append({"robot_id": "c", "status": "NAVIGATING", "x": sample["elapsed"], "y": 4})
        self.assertEqual(concurrency(samples)["three_robot_motion_seconds"], 1.0)


if __name__ == "__main__":
    unittest.main()
