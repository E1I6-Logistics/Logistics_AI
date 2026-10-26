"""Real executor integration; ROS sends, results and measured poses are controlled."""

import asyncio
import math
from time import monotonic
import unittest
from unittest.mock import patch

from backend.app.models.robot import RobotState, NavigationType
from backend.app.services.fleet_manager import FleetManager
from backend.app.services.real_navigation import RealNavigation
from backend.app.services.reservation import ReservationTable
from backend.app.services.pathfinding import DistanceAStar
from backend.app.services.mode_service import mode_manager
from backend.app.services.kev_mapf.kev_adapter import plan_mapf_from_route_results
from backend.app.services.kev_mapf.kev_coordinator import FmsMapfCoordinator
from backend.app.services.kev_mapf.kev_parallel_admission import fleet_snapshot
from backend.app.services.kev_mapf.kev_admission_recovery import admission_snapshot, apply_admission_recovery
from backend.app.services.kev_mapf.kev_recovery import KevRecovery
from backend.app.services.kev_mapf import kev_dispatcher
from tests.mock.kev_mock import MockKEV


def graph():
    points = {0: (0, 0), 1: (0, 3), 2: (0, 6), 7: (3, 3), 3: (6, 0), 4: (6, 3), 5: (6, 6)}
    edges = [(i, 7) for i in range(3)] + [(7, i) for i in range(3, 6)] + [(i, i+3) for i in range(3)]
    return {"type": "FeatureCollection", "features": [
        {"geometry": {"type": "Point", "coordinates": list(xy)}, "properties": {"id": str(n)}}
        for n, xy in points.items()
    ] + [
        {"geometry": {"type": "MultiLineString"}, "properties": {
            "id": f"e{i}", "startid": str(a), "endid": str(b), "weight": math.dist(points[a], points[b]),
        }} for i, (a, b) in enumerate(edges)
    ]}


class RealKevTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.graph = graph()
        self.nodes = DistanceAStar(self.graph).nodes
        self.now = monotonic()+1
        self.fleet = FleetManager()
        for i in range(3):
            robot = self.fleet.register_robot(f"robot{i+1}")
            robot.state = RobotState.IDLE
            robot.x, robot.y = self.nodes[str(i)]
            robot.yaw, robot.pose_received_at = 0., self.now
        self.sent, self.cancels, self.completed = [], [], []
        self.nav = RealNavigation(
            self.fleet, lambda **kw: self.sent.append(kw),
            lambda rid, callback: self.cancels.append((rid, callback)), self.complete,
            {}, .1, reservations=ReservationTable(), clock=lambda: self.now,
            mapf_ready=lambda rid: True,
        )
        self.coordinator = FmsMapfCoordinator(self.nav)
        for item in [patch("backend.app.services.real_navigation.load_route_graph", return_value=self.graph),
                     patch("backend.app.services.route_graph.load_route_graph", return_value=self.graph),
                     patch.object(mode_manager, "_mode", "real")]:
            item.start()
            self.addCleanup(item.stop)
        self.nav.refresh()

    def complete(self, rid):
        self.completed.append(rid)
        robot = self.fleet.get_robot(rid)
        robot.goal_node, robot.navigation_type = None, None
        robot.set_state(RobotState.IDLE)

    def advance(self, dt=.2):
        self.now += dt
        for robot in self.fleet.get_all_robots():
            if robot.connected:
                robot.pose_received_at = self.now
        self.nav.tick()

    def finish_action(self, rid):
        target = self.nav._executing[rid][1]
        robot = self.fleet.get_robot(rid)
        robot.x, robot.y = self.nodes[target]
        robot.pose_received_at = self.now
        self.nav.on_result(rid, True)

    def stage(self, shared=True):
        prepared = {}
        for i in range(3):
            path = [str(i), "7", str(i+3)] if shared else [str(i), str(i+3)]
            rid = f"robot{i+1}"
            req = self.coordinator.stage_preview({"robot_id": rid, "selector": "kev", "model": "kev-08b"},
                                                 path[0], path[-1], {"path": path})
            prepared[rid] = {"request": req, "result": req["result"], "graph": self.graph}
        snapshot = fleet_snapshot(self.nav.mapf, self.coordinator)
        advisory = plan_mapf_from_route_results(self.graph, snapshot["requests"], planner=MockKEV(),
                                              config={"wait_allowed_nodes": [0, 1, 2]})
        return prepared, snapshot, advisory

    def test_disjoint_cohort_starts_three_ros_actions_in_parallel(self):
        _, snapshot, advisory = self.stage(shared=False)
        self.assertTrue(self.coordinator.admission.apply(self.nav.mapf, self.graph, snapshot, advisory))
        self.assertEqual(len(self.nav._requests), 3)
        self.assertFalse(self.sent)
        self.advance()
        self.assertEqual(set(self.nav._executing), {"robot1", "robot2", "robot3"})

    def test_three_way_priority_is_enforced_before_each_ros_segment(self):
        _, snapshot, advisory = self.stage()
        self.assertTrue(self.coordinator.admission.apply(self.nav.mapf, self.graph, snapshot, advisory))
        self.advance()
        self.assertEqual(set(self.nav._executing), {"robot1"})
        self.assertEqual(len(self.sent[0]["waypoints"]), 1)
        self.finish_action("robot1")
        self.assertEqual(self.nav._robots["robot1"]["occupied_node"], "7")
        self.assertFalse(self.completed)
        self.advance()
        self.assertNotIn("robot2", self.nav._executing)
        for _ in range(1000):
            self.advance()
            for rid in list(self.nav._executing):
                self.finish_action(rid)
            if len(self.completed) == 3:
                break
        self.assertCountEqual(self.completed, ["robot1", "robot2", "robot3"])
        self.assertTrue(all(len(s["waypoints"]) == 1 for s in self.sent))

    def test_result_without_measured_arrival_does_not_advance_or_complete(self):
        self.nav.request("robot1", "3", NavigationType.GOAL, required_path=["0", "7", "3"])
        self.nav._send("robot1", "3", 0, DistanceAStar(self.graph))
        self.nav.on_result("robot1", True)
        self.assertEqual(self.nav._robots["robot1"]["route"]["segment_index"], 0)
        self.assertIn("robot1", self.nav._arrivals)
        self.assertFalse(self.completed)
        robot = self.fleet.get_robot("robot1")
        robot.x, robot.y = self.nodes["7"]
        self.nav._check_arrival("robot1")
        self.assertEqual(self.nav._robots["robot1"]["route"]["segment_index"], 1)
        self.assertFalse(self.completed)

    def test_stale_pose_or_pending_cancel_rejects_whole_cohort(self):
        _, snapshot, advisory = self.stage()
        before = self.nav._traffic._reservations.snapshot()
        self.fleet.get_robot("robot2").pose_received_at = self.now-10
        self.assertFalse(self.coordinator.admission.apply(self.nav.mapf, self.graph, snapshot, advisory))
        self.assertEqual(self.nav._traffic._reservations.snapshot(), before)
        self.assertFalse(self.nav._requests)
        self.fleet.get_robot("robot2").pose_received_at = self.now
        _, snapshot, advisory = self.stage()
        self.nav._stopping["robot2"] = None
        self.assertFalse(self.coordinator.admission.apply(self.nav.mapf, self.graph, snapshot, advisory))
        self.assertFalse(self.nav._requests)

    def test_cancellation_keeps_reservations_until_ros_terminal_callback(self):
        self.nav.request("robot1", "3", NavigationType.GOAL, required_path=["0", "7", "3"])
        self.nav._send("robot1", "3", 0, DistanceAStar(self.graph))
        before = self.nav._traffic._reservations.snapshot()
        self.nav.stop("robot1", lambda: self.nav.cancel_after_stop("robot1"))
        self.assertEqual(self.nav._traffic._reservations.snapshot(), before)
        self.assertIn("robot1", self.nav._executing)
        self.nav.refresh()
        self.assertTrue(self.nav._robots["robot1"]["execution_blocked"])
        self.cancels[-1][1]()
        self.assertFalse(self.nav._traffic._reservations.snapshot())
        self.assertNotIn("robot1", self.nav._requests)
        self.assertEqual(self.nav._robots["robot1"]["occupied_node"], "0")

    def test_changed_order_or_unregistered_ros_robot_cannot_commit_old_plan(self):
        _, snapshot, advisory = self.stage()
        self.fleet.get_robot("robot2").order_id = "replacement-order"
        self.assertFalse(self.coordinator.admission.apply(self.nav.mapf, self.graph, snapshot, advisory))
        _, snapshot, advisory = self.stage()
        self.nav._mapf_ready = lambda rid: rid != "robot2"
        self.assertFalse(self.coordinator.admission.apply(self.nav.mapf, self.graph, snapshot, advisory))
        self.assertFalse(self.nav._requests)
        self.assertFalse(self.nav._traffic._reservations.snapshot())

    def test_mode_change_before_commit_keeps_real_requests_unstarted(self):
        _, snapshot, advisory = self.stage()
        with patch.object(mode_manager, "_mode", "simulation"):
            with self.assertRaisesRegex(ValueError, "운용 모드"):
                self.coordinator.admission.apply(self.nav.mapf, self.graph, snapshot, advisory)
        self.assertFalse(self.nav._requests)
        self.assertFalse(self.sent)
        self.assertFalse(self.nav._traffic._reservations.snapshot())

    async def test_aside_arrival_keeps_order_then_kev_resume_completes_goal(self):
        self.fleet.get_robot("robot1").order_id = "business-order"
        prepared, _, _ = self.stage()
        snapshot = admission_snapshot(self.nav, self.coordinator)
        proposal = ("robot1", ["0", "7"], {"choice": "aside", "action": "MOVE_ASIDE", "temporary_goal": "7"})
        result = apply_admission_recovery(self.nav, self.coordinator, self.graph, snapshot, proposal,
                                         robot_id="robot1", prepared=prepared["robot1"], valid=lambda: True)
        self.assertIsNotNone(result)
        self.advance()
        self.finish_action("robot1")
        self.assertEqual(self.nav._robots["robot1"]["kev_maneuver"]["phase"], "ASIDE")
        self.assertEqual(self.nav._requests["robot1"][0], "3")
        self.assertFalse(self.completed)
        self.assertEqual(self.fleet.get_robot("robot1").order_id, "business-order")
        self.advance()
        self.assertNotIn("robot1", self.nav._executing)
        worker = KevRecovery(stall_seconds=.1, retry_seconds=.1)
        with patch.object(worker.planner, "plan", return_value=("robot1", ["7", "3"], {"choice": "resume", "action": "RESUME"})):
            await worker.run_once(self.nav, self.coordinator, mode_manager, now=self.now)
            self.advance()
            self.assertTrue(await worker.run_once(self.nav, self.coordinator, mode_manager, now=self.now))
        for _ in range(1000):
            self.advance()
            if "robot1" in self.nav._executing:
                self.finish_action("robot1")
            if self.completed:
                break
        self.assertEqual(self.completed, ["robot1"])
        self.assertEqual(self.nav._robots["robot1"]["occupied_node"], "3")

    async def test_real_orders_use_common_preparation_and_atomic_admission(self):
        def preview(graph, start, goal, **kwargs):
            return {"path": [str(start), "7", str(goal)]}

        def plan(graph, requests, *, config):
            return plan_mapf_from_route_results(graph, requests, planner=MockKEV(), config=config)

        with patch.object(kev_dispatcher, "runtime_for", return_value=(self.nav, self.coordinator)), patch.object(
            kev_dispatcher, "load_route_graph", return_value=self.graph
        ), patch("backend.app.services.llm_route_preview.preview_llm_route", side_effect=preview), patch(
            "backend.app.services.kev_mapf.kev_parallel_admission.plan_mapf_from_route_results", side_effect=plan
        ):
            results = await asyncio.gather(*[
                kev_dispatcher.dispatch_mapf({"robot_id": f"robot{i+1}", "mode": "real", "operation_mode": "ai",
                                             "driving_mode": "llm", "selector": "kev", "model": "kev-08b"},
                                            str(i+3), str(i), valid=lambda: True)
                for i in range(3)
            ])
        self.assertEqual(len({r["mapf"]["plan_id"] for r in results}), 1)
        self.assertTrue(all(r["source"] == "ros2" and r["mapf"]["priority_mode"] == "score" for r in results))
        self.assertEqual(len(self.nav._requests), 3)
