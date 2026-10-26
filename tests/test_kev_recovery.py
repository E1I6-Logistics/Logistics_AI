"""KEV selects recovery; FMS preserves tasks and rejects unsafe/stale plans."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend.app.services.mock_data import MockFmsStore
from backend.app.services.reservation import ReservationTable
from backend.app.services.kev_mapf.kev_coordinator import FmsMapfCoordinator
from backend.app.services.kev_mapf.kev_recovery import KevRecovery
from backend.app.services.kev_mapf.kev_client import LiveKEV
from backend.app.services.kev_mapf.kev_engine import KevMAPF
from backend.app.services.kev_mapf.kev_graph import Graph
from tests.mock.kev_mock import MockKEV

ROOT = Path(__file__).resolve().parents[1]
DETOUR = ["9", "10", "8", "12", "11", "6"]


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.graph = json.loads((ROOT / "routes/test.geojson").read_text())
        self.store = MockFmsStore(ReservationTable())
        self.coordinator = FmsMapfCoordinator(self.store)
        self.worker = KevRecovery(stall_seconds=1, retry_seconds=5)
        self.mode = SimpleNamespace(mode="simulation")
        nodes = Graph(self.graph).nodes
        self.store._robots.clear()
        for i, (node, path) in enumerate([
            (5, [5, 7, 11, 3]), (7, [7, 9, 1]), (9, [9, 7, 11, 6]),
        ], 1):
            rid = f"robot{i}"
            path = list(map(str, path))
            self.store._robots[rid] = dict(
                robot_id=rid, x=nodes[node][0], y=nodes[node][1], yaw=0,
                current_node=str(node), occupied_node=str(node), occupied_edge=None,
                status="WAITING", connection_state="ONLINE", stop_requested=False,
                navigation_id=f"nav{i}", request_order=i, task_priority=i,
                goal_node=path[-1], route={"node_ids": path, "segment_index": 0,
                                         "departure_at": None, "phase": "waiting"},
            )
            self.store._traffic._required_paths[rid] = tuple(path)
            self.store._traffic._pending[rid] = None
            self.coordinator.register_preview(
                {"robot_id": rid, "selector": "kev", "model": "kev-08b"},
                node, path[-1], {"path": path},
            )
        self.store._last_tick = 100
        self.preview = self.enterContext(patch(
            "backend.app.services.kev_mapf.kev_recovery.preview_llm_route",
            return_value={"path": DETOUR},
        ))
        self.selector_cls = self.enterContext(patch(
            "backend.app.services.kev_mapf.kev_recovery.KevSelector"
        ))
        self.choice = self.selector_cls.return_value.select_choice
        self.choice.return_value = {"choice": "reroute_robot3", "confidence": .8}
        self.enterContext(patch("backend.app.services.route_graph.load_route_graph", return_value=self.graph))
        self.enterContext(patch("backend.app.services.mock_data.load_route_graph", return_value=self.graph))
        self.enterContext(patch("backend.app.services.kev_mapf.kev_recovery.monotonic", return_value=102))

    async def recover(self):
        self.assertFalse(await self.worker.run_once(self.store, self.coordinator, self.mode, now=100))
        return await self.worker.run_once(self.store, self.coordinator, self.mode, now=102)

    async def test_screenshot_recovers_all_three_with_original_tasks(self):
        originals = deepcopy(self.store._robots)
        self.assertTrue(await self.recover())
        self.choice.assert_called_once()
        for key in ("navigation_id", "request_order", "task_priority", "goal_node", "x", "y"):
            self.assertEqual(self.store._robots["robot3"][key], originals["robot3"][key])
        self.assertEqual(self.coordinator._route_requests["robot3"]["result"]["path"], DETOUR)
        self.assertEqual(self.store._traffic._required_paths["robot1"], ("5", "7", "11", "3"))
        for tick in range(1, 601):
            self.store.advance_simulation(.1, now=102 + tick * .1)
            if all(r["goal_node"] is None for r in self.store._robots.values()):
                break
        self.assertEqual(
            {rid: r["occupied_node"] for rid, r in self.store._robots.items()},
            {"robot1": "3", "robot2": "1", "robot3": "6"},
        )
        self.assertTrue(all(r["status"] == "IDLE" for r in self.store._robots.values()))

    async def test_unchanged_routes_stay_blocked_without_recovery(self):
        before = deepcopy(self.store._robots)
        for tick in range(100):
            self.store._traffic.retry_waiting(100 + tick * .2, self.graph)
        self.assertEqual(self.store._robots, before)

    async def test_wait_does_not_replace_a_route(self):
        self.choice.return_value = {"choice": "wait"}
        before = self.worker._capture(self.store, self.coordinator)
        self.assertFalse(await self.recover())
        self.assertTrue(self.preview.called)  # Read-only candidates are supplied to KEV.
        self.assertEqual(self.worker._capture(self.store, self.coordinator), before)

    async def test_model_failure_keeps_state_and_backs_off(self):
        self.choice.side_effect = RuntimeError("KEV offline")
        before = self.worker._capture(self.store, self.coordinator)
        with self.assertLogs("backend.app.services.kev_mapf.kev_recovery", level="ERROR"):
            self.assertFalse(await self.recover())
        self.assertFalse(await self.worker.run_once(self.store, self.coordinator, self.mode, now=103))
        self.choice.assert_called_once()
        self.assertEqual(self.worker._capture(self.store, self.coordinator), before)

    async def test_rejected_route_evidence_is_supplied_to_kev(self):
        def preview(graph, start, goal, **kwargs):
            if str(start) == "9":
                return {"path": DETOUR}
            raise ValueError("Current occupancy blocks this route")
        self.preview.side_effect = preview
        self.assertTrue(await self.recover())
        self.choice.assert_called_once()
        state, choices, _ = self.choice.call_args.args
        self.assertIn("reroute_robot2", [item["action"] for item in state["rejected_attempts"]])
        self.assertNotIn("reroute_robot2", choices)

    async def test_path_through_other_occupancy_is_rejected(self):
        self.choice.return_value = {"choice": "wait"}
        self.preview.return_value = {"path": ["9", "7", "11", "6"]}
        before = self.worker._capture(self.store, self.coordinator)
        self.assertFalse(await self.recover())
        self.assertEqual(self.worker._capture(self.store, self.coordinator), before)

    def second_junction(self):
        """Second screenshot: N8 follower, N9/N7 swap, no full detour."""
        nodes = Graph(self.graph).nodes
        for rid, path in {
            "robot1": ["8", "7", "11", "6"],
            "robot2": ["9", "7", "5"],
            "robot3": ["7", "9", "2"],
        }.items():
            robot = self.store._robots[rid]
            robot.update(
                x=nodes[int(path[0])][0], y=nodes[int(path[0])][1],
                current_node=path[0], occupied_node=path[0], goal_node=path[-1],
                route={"node_ids": path, "segment_index": 0, "departure_at": None, "phase": "waiting"},
            )
            self.store._traffic._required_paths[rid] = tuple(path)
            self.coordinator.register_preview(
                {"robot_id": rid, "selector": "kev", "model": "kev-08b"},
                path[0], path[-1], {"path": path},
            )

    async def test_second_screenshot_move_aside_and_resume_preserve_original_tasks(self):
        self.second_junction()
        self.preview.side_effect = ValueError("No route avoiding the current occupants")
        self.choice.return_value = {"choice": "move_aside_robot2_10"}
        self.assertTrue(await self.recover())
        mover = self.store._robots["robot2"]
        self.assertEqual(mover["goal_node"], "5")
        self.assertEqual(mover["navigation_id"], "nav2")
        self.assertEqual(mover["kev_recovery"]["action"], "MOVE_ASIDE")
        with patch("backend.app.services.mock_data.mock_fms", self.store):
            self.assertEqual(
                self.coordinator.inspect_state()["robot2"]["issue"],
                "KEV_MOVE_ASIDE_ACTIVE",
            )
        for tick in range(1, 201):
            now = 102 + tick * .1
            self.store.advance_simulation(.1, now=now)
        self.assertEqual(mover["occupied_node"], "10")
        self.assertEqual(mover["kev_maneuver"]["phase"], "ASIDE")
        self.assertEqual(mover["status"], "WAITING")
        self.assertEqual(mover["goal_node"], "5")  # Parking is never order completion.
        self.assertEqual(self.store._robots["robot1"]["occupied_node"], "6")
        self.assertEqual(self.store._robots["robot3"]["occupied_node"], "2")
        self.preview.side_effect = None
        self.preview.return_value = {"path": ["10", "9", "7", "5"]}
        self.choice.return_value = {"choice": "resume_robot2"}
        with patch("backend.app.services.kev_mapf.kev_recovery.monotonic", return_value=now+2):
            self.assertFalse(await self.worker.run_once(self.store, self.coordinator, self.mode, now=now))
            self.assertTrue(await self.worker.run_once(self.store, self.coordinator, self.mode, now=now+2))
        self.assertNotIn("kev_maneuver", mover)
        for tick in range(1, 201):
            self.store.advance_simulation(.1, now=now+2+tick*.1)
        self.assertEqual(mover["occupied_node"], "5")
        self.assertTrue(all(r["goal_node"] is None for r in self.store._robots.values()))

    async def test_kev_can_choose_the_other_robot_to_step_aside(self):
        self.second_junction()
        self.preview.side_effect = ValueError("No full detour")
        self.choice.return_value = {"choice": "move_aside_robot3_11"}
        self.assertTrue(await self.recover())
        for tick in range(1, 201):
            now = 102 + tick * .1
            self.store.advance_simulation(.1, now=now)
        self.assertEqual(self.store._robots["robot3"]["kev_maneuver"]["phase"], "ASIDE")
        self.preview.side_effect = None
        self.preview.return_value = {"path": ["11", "7", "9", "2"]}
        self.choice.return_value = {"choice": "resume_robot3"}
        with patch("backend.app.services.kev_mapf.kev_recovery.monotonic", return_value=now+2):
            await self.worker.run_once(self.store, self.coordinator, self.mode, now=now)
            self.assertTrue(await self.worker.run_once(self.store, self.coordinator, self.mode, now=now+2))
        for tick in range(1, 201):
            self.store.advance_simulation(.1, now=now+2+tick*.1)
        self.assertEqual(
            {rid: r["occupied_node"] for rid, r in self.store._robots.items()},
            {"robot1": "6", "robot2": "5", "robot3": "2"},
        )
        self.assertTrue(all(r["goal_node"] is None for r in self.store._robots.values()))

    async def test_cancelled_temporary_maneuver_never_resumes(self):
        self.second_junction()
        self.preview.side_effect = ValueError("No full detour")
        self.choice.return_value = {"choice": "move_aside_robot2_10"}
        self.assertTrue(await self.recover())
        self.store._traffic.cancel_navigation_after_stop("robot2")
        self.assertNotIn("kev_maneuver", self.store._robots["robot2"])
        self.assertIsNone(self.store._robots["robot2"]["goal_node"])
        captured = self.worker._capture(self.store, self.coordinator)
        self.assertNotIn("robot2", self.worker._candidates(captured, 104))

    def test_legal_neighbors_are_exposed_without_code_ranking(self):
        self.second_junction()
        snapshot = self.worker._capture(self.store, self.coordinator)
        blocked = self.worker._candidates(snapshot, 102)
        candidates = self.worker.planner._aside_candidates(self.graph, snapshot, blocked)
        self.assertIn("move_aside_robot2_10", candidates)
        self.assertIn("move_aside_robot3_11", candidates)
        # KEV must assess whether the follower should move; code does not
        # discard it by calculating which maneuver releases another route.
        self.assertIn("move_aside_robot1_10", candidates)
        self.assertIn("move_aside_robot1_12", candidates)
        self.assertFalse(any(path[-1] in ("8", "9", "7", "5", "6", "2")
                             for _, path, _ in candidates.values()))

    async def test_kev_selects_actor_and_node_without_code_override(self):
        self.second_junction()
        self.preview.side_effect = ValueError("No full detour")
        # Even a follower's legal move is decided by KEV. The code must not
        # replace it with a pre-ranked robot3/robot2 maneuver.
        self.choice.return_value = {"choice": "move_aside_robot1_12"}
        self.assertTrue(await self.recover())
        self.assertEqual(self.store._robots["robot1"]["kev_maneuver"]["target_node"], "12")
        state, choices, _ = self.choice.call_args.args
        self.assertEqual({r["id"] for r in state["robots"]}, {"robot1", "robot2", "robot3"})
        self.assertEqual(next(r for r in state["robots"] if r["id"] == "robot3")["remaining_path"], ["7", "9", "2"])
        self.assertIn("move_aside_robot2_10", choices)
        self.assertIn("move_aside_robot3_11", choices)
        self.assertFalse(any("unblocks" in action for action in state["valid_maneuvers"]))

    async def test_reservation_time_refresh_is_not_robot_progress(self):
        await self.worker.run_once(self.store, self.coordinator, self.mode, now=100)
        self.store._robots["robot2"]["route"]["segment_departures"] = [200, 210]
        self.store._robots["robot2"]["route"]["phase"] = "ready"
        self.assertTrue(await self.worker.run_once(self.store, self.coordinator, self.mode, now=102))

    async def test_reservation_refresh_during_inference_revalidates_latest_table(self):
        def preview(graph, start, goal, **kwargs):
            self.store._robots["robot2"]["route"]["segment_departures"] = [300, 310]
            return {"path": DETOUR}
        self.preview.side_effect = preview
        self.assertTrue(await self.recover())

    async def test_kev_resume_hold_preserves_temporary_state_and_original_goal(self):
        self.second_junction()
        self.preview.side_effect = ValueError("No full detour")
        self.choice.return_value = {"choice": "move_aside_robot3_11"}
        self.assertTrue(await self.recover())
        for tick in range(1, 201):
            self.store.advance_simulation(.1, now=102+tick*.1)
        self.preview.side_effect = None
        self.preview.return_value = {"path": ["11", "7", "9", "2"]}
        self.choice.return_value = {"choice": "wait"}
        before = self.worker._capture(self.store, self.coordinator)
        await self.worker.run_once(self.store, self.coordinator, self.mode, now=123)
        self.assertFalse(await self.worker.run_once(self.store, self.coordinator, self.mode, now=125))
        self.assertEqual(self.worker._capture(self.store, self.coordinator), before)
        self.assertEqual(self.store._robots["robot3"]["goal_node"], "2")

    async def test_stop_during_resume_permission_does_not_restart_a_task(self):
        self.second_junction()
        self.preview.side_effect = ValueError("No full detour")
        self.choice.return_value = {"choice": "move_aside_robot3_11"}
        self.assertTrue(await self.recover())
        for tick in range(1, 201):
            self.store.advance_simulation(.1, now=102+tick*.1)
        self.preview.side_effect = None
        self.preview.return_value = {"path": ["11", "7", "9", "2"]}
        def cancel(*args, **kwargs):
            self.store._traffic.cancel_navigation_after_stop("robot3")
            return {"choice": "resume_robot3"}
        self.choice.side_effect = cancel
        await self.worker.run_once(self.store, self.coordinator, self.mode, now=123)
        self.assertFalse(await self.worker.run_once(self.store, self.coordinator, self.mode, now=125))
        self.assertIsNone(self.store._robots["robot3"]["goal_node"])
        self.assertNotIn("kev_maneuver", self.store._robots["robot3"])

    async def test_stop_during_inference_does_not_resurrect_navigation(self):
        def stop(*args, **kwargs):
            self.store._traffic.cancel_navigation_after_stop("robot3")
            return {"path": DETOUR}
        self.preview.side_effect = stop
        self.assertFalse(await self.recover())
        self.assertIsNone(self.store._robots["robot3"]["goal_node"])
        self.assertEqual(self.store._robots["robot3"]["status"], "IDLE")
        self.assertIsNone(self.store._robots["robot3"].get("kev_recovery"))

    async def test_changed_map_or_mode_discards_result(self):
        for change in ("map", "mode"):
            with self.subTest(change=change):
                self.worker = KevRecovery(stall_seconds=1)
                def mutate(*args, **kwargs):
                    if change == "map":
                        self.graph["revision"] = "changed"
                    else:
                        self.mode.mode = "real"
                    return {"path": DETOUR}
                # A fresh graph value is returned on each read, as in production.
                with patch("backend.app.services.route_graph.load_route_graph", side_effect=lambda: deepcopy(self.graph)):
                    self.preview.side_effect = mutate
                    self.assertFalse(await self.recover())
                self.mode.mode = "simulation"
                self.assertEqual(self.store._traffic._required_paths["robot3"], ("9", "7", "11", "6"))

    async def test_mid_edge_and_algorithm_requests_are_not_replanned(self):
        self.store._robots["robot3"].update(occupied_node=None, occupied_edge="30")
        self.coordinator._route_requests["robot1"]["order"]["selector"] = "laya"
        self.coordinator._route_requests["robot2"]["order"]["selector"] = "laya"
        self.assertFalse(await self.recover())
        self.choice.assert_not_called()

    async def test_overlapping_recovery_attempt_does_not_infer(self):
        self.worker._running = True
        self.assertFalse(await self.worker.run_once(self.store, self.coordinator, self.mode, now=102))
        self.choice.assert_not_called()

    def test_failed_reservation_does_not_change_request_or_reservations(self):
        self.store._robots["robot2"].update(current_node="11", occupied_node="11")
        before = self.worker._capture(self.store, self.coordinator)
        reservations = self.store._traffic._reservations.snapshot()
        self.assertFalse(self.store._traffic.replace_required_path_after_stop(
            "robot3", DETOUR, now=102, graph=self.graph
        ))
        self.assertEqual(self.worker._capture(self.store, self.coordinator), before)
        self.assertEqual(self.store._traffic._reservations.snapshot(), reservations)

    def test_zone_hold_without_yield_robot_returns_blocked(self):
        robots = {
            rid: dict(id=rid, current_node=int(r["occupied_node"]), goal_node=int(r["goal_node"]),
                      path=list(map(int, self.store._traffic._required_paths[rid])),
                      selector="kev", model="kev-08b", source="test", task_priority=0)
            for rid, r in self.store._robots.items()
        }
        result = KevMAPF(Graph(self.graph), robots, MockKEV(), {
            "zone_occupancy": {f"node:{r['current_node']}": rid for rid, r in robots.items()},
        }).resolve()
        self.assertEqual(result["status"], "BLOCKED")


class StrictKevTests(unittest.TestCase):
    def test_no_algorithm_fallback_on_inference_or_route_failure(self):
        live = LiveKEV(model="kev-08b")
        live.selector = Mock()
        live.selector.decide.side_effect = RuntimeError("offline")
        live.selector.select_choice.side_effect = RuntimeError("offline")
        live.preview_llm_route = Mock(side_effect=ValueError("invalid model route"))
        for call in (
            lambda: live.score_many({}, ["robot1", "robot2"]),
            lambda: live.pick({}, {"a": "A", "b": "B"}, "choose", "pick"),
            lambda: live.route({}, 1, 2),
        ):
            with self.assertRaises((RuntimeError, ValueError)):
                call()


if __name__ == "__main__":
    unittest.main()
