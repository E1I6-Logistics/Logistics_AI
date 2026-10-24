"""실제 지도에서 여러 로봇의 양보·통과·복귀와 중단을 검증한다."""

import asyncio
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.app.services.mock_data import MockFmsStore
from backend.app.services.pathfinding import DistanceAStar
from backend.app.services.reservation import ReservationTable
from backend.app.services.route_graph import load_route_graph
from backend.app.services.scenario_service import ScenarioManager, load_config
from backend.app.services.traffic_joint_actions import TrafficSituation
from backend.app.services.traffic_situation_adapter import build_traffic_situation_input
from backend.simulation.traffic_joint_action_scenario import resolve_route_source
from backend.simulation.traffic_joint_actions import JointActionSimulation, TestSelector as FakeSelector


def scenario():
    return next(s for s in load_config()["scenarios"] if s["name"] == "fixed_two_robot_yield")


def payload():
    case = scenario()
    states = {}
    for spec in case["robots"]:
        states[spec["id"]] = dict(
            occupied_node=spec["start"], status="IDLE", occupied_edge=None,
            navigation_id=None, route=None, goal_node=None, connected=True, fresh=True, active=False,
        )
    return build_traffic_situation_input(
        graph=load_route_graph(), snapshot_id="fixed-plan-test", mode="simulation",
        route_requests=[dict(order=dict(robot_id="robot1", selector="kev", model="kev-08b"),
                             current_node=case["robots"][0]["start"], node=case["robots"][0]["goal"],
                             result=dict(path=case["traffic_joint_action"]["paths"]["robot1"]))],
        robot_states=states, reservations=[], protected_resources={},
        request_metadata={"robot1": dict(request_order=1, waiting_seconds=0)},
        wait_allowed_nodes=case["traffic_joint_action"]["wait_allowed_nodes"],
        config=dict(fixed_plan=case["traffic_joint_action"]["fixed_plan"]),
    )


class FixedPlanTests(unittest.TestCase):
    def test_five_steps_use_existing_reservations_without_model(self):
        scene = TrafficSituation(payload())
        selector = FakeSelector()
        decision = scene.decide(selector)
        self.assertEqual(selector.calls, 0)
        self.assertEqual(decision.selection_source, "FIXED_SCENARIO")
        simulator = JointActionSimulation(scene)
        result = simulator.execute(decision)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["steps_completed"], 5)
        self.assertEqual(result["final_nodes"], {"robot1": "0", "robot2": "11", "robot3": "10"})
        self.assertGreater(result["capacity_checks"], 0)
        self.assertEqual(result["capacity_violations"], 0)
        self.assertFalse(simulator.store._traffic._reservations.snapshot())

    def test_invalid_or_unfinished_fixed_plans_are_rejected(self):
        changes = [
            lambda p: p["config"]["fixed_plan"]["steps"][0].update(robot_id="missing"),
            lambda p: p["config"]["fixed_plan"]["steps"][0].update(action="TELEPORT"),
            lambda p: p["config"]["fixed_plan"]["steps"][0].update(path=["9", "0"]),
            lambda p: p["config"]["fixed_plan"]["steps"][0].update(path=["1", "2"]),
            lambda p: p["config"]["wait_allowed_nodes"].remove("6"),
            lambda p: p["config"]["fixed_plan"]["steps"].pop(),
            lambda p: p["config"]["fixed_plan"]["steps"].pop(0),
            lambda p: p["config"].update(blocked_edges=[["6", "11"]]),
            lambda p: p["config"]["fixed_plan"].update(steps=[]),
        ]
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                value = payload()
                change(value)
                with self.assertRaises(ValueError):
                    TrafficSituation(value)

    def test_fixed_selector_requires_explicit_plan_and_fixed_routes(self):
        for options in (
            {"traffic_selector": "fixed"},
            {"fixed_plan": {"id": "x", "steps": []}},
            {**scenario()["traffic_joint_action"], "route_source": "KEV"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                resolve_route_source(options, "ai")

    def test_stop_after_first_arrival_prevents_later_steps_and_stops_fleet(self):
        self._run_failed_scenario(interrupt=True)

    def test_timeout_stops_fleet_without_starting_later_steps(self):
        self._run_failed_scenario(interrupt=False)

    def _run_failed_scenario(self, *, interrupt):
        case = deepcopy(scenario())
        if not interrupt:
            case["traffic_joint_action"]["timeout_seconds"] = 0.03
        graph = load_route_graph()
        store = MockFmsStore(ReservationTable())
        manager = ScenarioManager()
        manager._status = dict(state="running", mode="simulation", operation_mode="ai",
                               run_id=f"fixed-failure-{interrupt}", message="", robot_ids=list(store._robots))
        commands = []

        def navigate(rid, goal, **kwargs):
            commands.append((rid, goal))
            if interrupt:
                # 첫 양보 도착 직후 들어온 중단 요청. 뒤 네 단계는 전송하지 않는다.
                store._robots[rid].update(status="IDLE", occupied_node=goal, current_node=goal,
                                          route=None, goal_node=None, occupied_edge=None)
                manager.interrupt(rid)
            # timeout 시험은 도착하지 않은 상태로 둔다.

        mode = SimpleNamespace(mode="simulation", operation_mode="ai")
        with TemporaryDirectory() as folder, patch("backend.app.config.BASE_DIR", Path(folder)), patch(
            "backend.app.services.mock_data.mock_fms", store
        ), patch("backend.simulation.scenarios.mock_fms", store), patch(
            "backend.app.services.scenario_service.mode_manager", mode
        ), patch("backend.app.services.mode_service.mode_manager", mode), patch.object(
            store, "navigate_to_node", side_effect=navigate
        ), patch.object(store, "stop_robot", wraps=store.stop_robot) as stop, patch(
            "backend.app.services.route_selector.kev_selector.KevSelector", side_effect=AssertionError("KEV 호출 금지")
        ):
            asyncio.run(manager._run(case, graph, DistanceAStar(graph).nodes, 90))
            self.assertEqual(manager.status()["state"], "failed")
            self.assertIn("중단" if interrupt else "미완료", manager.status()["message"])
            self.assertEqual(commands, [("robot2", "6")])
            self.assertTrue(set(store._robots) <= {call.args[0] for call in stop.call_args_list})
            self.assertTrue(all(r["route"] is None and r["goal_node"] is None for r in store._robots.values()))
            self.assertFalse(store._traffic._reservations.snapshot())
            self.assertEqual(manager.status()["coordination"]["actual_metrics"]["outcome"], "INTERRUPTED")


if __name__ == "__main__":
    unittest.main()
