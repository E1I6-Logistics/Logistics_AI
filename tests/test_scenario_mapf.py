"""[테스트 모듈: test_scenario_mapf.py - 트래픽 시나리오의 알고리즘 vs AI MAPF 분기 실행 검증]"""

import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from backend.app.services.scenario_service import load_config
from backend.app.services.mode_service import mode_manager
from backend.app.services.route_graph import load_route_graph
from tests.mock.kev_mock import MockKEV


class ScenarioMapfTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.scenarios = {s["name"]: s for s in self.config["scenarios"]}

    def test_scenario_config_schema(self):
        """traffic_scenarios.json에 5쌍의 알고리즘·AI/MAPF 시나리오와 주문 스트레스 시험가 올바르게 정의되어 있는지 검증."""
        expected_pairs = [
            ("corridor_head_on", "corridor_head_on_ai"),
            ("junction_contention", "junction_contention_ai"),
            ("bottleneck", "bottleneck_ai"),
            ("deadlock_cycle", "deadlock_cycle_ai"),
            ("priority_yield", "priority_yield_ai"),
        ]

        self.assertEqual(len(self.scenarios), 11)
        for algo_name, ai_name in expected_pairs:
            self.assertIn(algo_name, self.scenarios, f"{algo_name} 누락")
            self.assertIn(ai_name, self.scenarios, f"{ai_name} 누락")

            algo = self.scenarios[algo_name]
            self.assertEqual(algo["operation_mode"], "algorithm")
            self.assertIn("[알고리즘]", algo["description"])

            ai = self.scenarios[ai_name]
            self.assertEqual(ai["operation_mode"], "ai")
            self.assertIn("[AI/MAPF]", ai["description"])
            self.assertIsInstance(ai.get("wait_allowed_nodes"), list)
            for r in ai["robots"]:
                if r["goal"] is not None:
                    self.assertEqual(r["driving_mode"], "llm")
                    self.assertEqual(r["selector"], "kev")
                    self.assertIn("task_priority", r)

    def test_corridor_head_on_algorithm_keeps_standard_dispatch(self):
        """[알고리즘] corridor_head_on 시나리오 실행 시 required_path 없이 표준 알고리즘 배차 호출 검증."""
        calls = []

        def fake_navigate(rid, goal, mode, **kwargs):
            calls.append({"rid": rid, "goal": goal, "mode": mode, **kwargs})
            return {"success": True}

        scenario = self.scenarios["corridor_head_on"]
        graph = load_route_graph()
        nodes = {str(n): (0.0, 0.0) for n in range(13)}

        with patch("backend.app.services.scenario_service.navigate_to_node", side_effect=fake_navigate):
            import asyncio
            asyncio.run(
                self._run_single_step(scenario, graph, nodes)
            )

        self.assertEqual(len(calls), 2)  # robot1, robot2
        for c in calls:
            self.assertNotIn("required_path", c)

    def test_corridor_head_on_ai_executes_kev_mapf(self):
        """[AI/MAPF] corridor_head_on_ai 시나리오 실행 시 KEV-MAPF 조정을 거쳐 required_path로 배차되는지 검증."""
        calls = []

        def fake_navigate(rid, goal, mode, **kwargs):
            calls.append({"rid": rid, "goal": goal, "mode": mode, **kwargs})
            return {"success": True}

        scenario = self.scenarios["corridor_head_on_ai"]
        graph = load_route_graph()
        nodes = {str(n): (0.0, 0.0) for n in range(13)}

        with patch("backend.app.services.scenario_service.navigate_to_node", side_effect=fake_navigate):
            import asyncio
            asyncio.run(
                self._run_single_step(scenario, graph, nodes)
            )

        self.assertEqual(len(calls), 2)  # robot1, robot2
        for c in calls:
            self.assertIn("required_path", c)
            self.assertIsInstance(c["required_path"], list)
            self.assertGreater(len(c["required_path"]), 1)

    def test_junction_contention_ai_coordinates_three_robots(self):
        """[AI/MAPF] junction_contention_ai 3대 로봇 교차로 경합 시 KEV-MAPF 조정 및 배차 검증."""
        calls = []

        def fake_navigate(rid, goal, mode, **kwargs):
            calls.append({"rid": rid, "goal": goal, "mode": mode, **kwargs})
            return {"success": True}

        scenario = self.scenarios["junction_contention_ai"]
        graph = load_route_graph()
        nodes = {str(n): (0.0, 0.0) for n in range(13)}

        with patch("backend.app.services.scenario_service.navigate_to_node", side_effect=fake_navigate):
            import asyncio
            asyncio.run(
                self._run_single_step(scenario, graph, nodes)
            )

        self.assertEqual(len(calls), 3)  # robot1, robot2, robot3
        for c in calls:
            self.assertIn("required_path", c)
            self.assertIsInstance(c["required_path"], list)

    def test_bottleneck_ai_coordinates_three_robots(self):
        """[AI/MAPF] bottleneck_ai 3대 로봇 병목 합류 시 KEV-MAPF 조정 및 배차 검증."""
        calls = []

        def fake_navigate(rid, goal, mode, **kwargs):
            calls.append({"rid": rid, "goal": goal, "mode": mode, **kwargs})
            return {"success": True}

        scenario = self.scenarios["bottleneck_ai"]
        graph = load_route_graph()
        nodes = {str(n): (0.0, 0.0) for n in range(13)}

        with patch("backend.app.services.scenario_service.navigate_to_node", side_effect=fake_navigate):
            import asyncio
            asyncio.run(
                self._run_single_step(scenario, graph, nodes)
            )

        self.assertEqual(len(calls), 3)  # robot1, robot2, robot3
        for c in calls:
            self.assertIn("required_path", c)
            self.assertIsInstance(c["required_path"], list)

    def test_deadlock_cycle_ai_coordinates_three_robots(self):
        """[AI/MAPF] deadlock_cycle_ai 3대 로봇 순환 교착 시 KEV-MAPF 조정 및 배차 검증."""
        calls = []

        def fake_navigate(rid, goal, mode, **kwargs):
            calls.append({"rid": rid, "goal": goal, "mode": mode, **kwargs})
            return {"success": True}

        scenario = self.scenarios["deadlock_cycle_ai"]
        graph = load_route_graph()
        nodes = {str(n): (0.0, 0.0) for n in range(13)}

        with patch("backend.app.services.scenario_service.navigate_to_node", side_effect=fake_navigate):
            import asyncio
            asyncio.run(
                self._run_single_step(scenario, graph, nodes)
            )

        self.assertEqual(len(calls), 3)
        for c in calls:
            self.assertIn("required_path", c)
            self.assertIsInstance(c["required_path"], list)

    def test_priority_yield_ai_coordinates_three_robots(self):
        """[AI/MAPF] priority_yield_ai 긴급 화물 로봇과 3대 동시 대피 조정 및 배차 검증."""
        calls = []

        def fake_navigate(rid, goal, mode, **kwargs):
            calls.append({"rid": rid, "goal": goal, "mode": mode, **kwargs})
            return {"success": True}

        scenario = self.scenarios["priority_yield_ai"]
        graph = load_route_graph()
        nodes = {str(n): (0.0, 0.0) for n in range(13)}

        with patch("backend.app.services.scenario_service.navigate_to_node", side_effect=fake_navigate):
            import asyncio
            asyncio.run(
                self._run_single_step(scenario, graph, nodes)
            )

        self.assertEqual(len(calls), 3)
        for c in calls:
            self.assertIn("required_path", c)
            self.assertIsInstance(c["required_path"], list)

    async def _run_single_step(self, scenario, graph, nodes):
        """테스트를 위해 _run의 초기 배차 블록만 실행하고 대기 루프 전 반환."""
        from backend.app.services.scenario_service import navigate_to_node
        from backend.app.services.pathfinding import DistanceAStar
        from backend.app.services.kev_mapf import plan_mapf_from_route_results

        specs = scenario["robots"]
        goals = {r["id"]: r["goal"] for r in specs if r["goal"] is not None}
        mode = "simulation"
        operation_mode = scenario.get("operation_mode") or mode_manager.operation_mode
        is_ai = operation_mode == "ai" or any(r.get("driving_mode") == "llm" for r in specs)

        if is_ai:
            route_planner = DistanceAStar(graph)
            route_results = []
            for spec in specs:
                if spec.get("goal") is None:
                    continue
                rid, start, goal = spec["id"], str(spec["start"]), str(spec["goal"])
                plan_res = route_planner.plan(start, goal)
                path = [str(n) for n in getattr(plan_res, "route", getattr(plan_res, "node_ids", []))]
                route_results.append({
                    "order": {
                        "robot_id": rid,
                        "selector": spec.get("selector", "kev"),
                        "model": spec.get("model", "kev-08b"),
                    },
                    "current_node": start,
                    "node": goal,
                    "result": {"path": path},
                    "task_priority": int(spec.get("task_priority", 0)),
                })
            advisory = plan_mapf_from_route_results(
                graph,
                route_results,
                planner=MockKEV(),
                config={
                    "wait_allowed_nodes": scenario.get("wait_allowed_nodes", ["0", "10", "7", "8"]),
                    "priority_mode": "score",
                },
            )
            assigned_paths = {
                rid: [str(n) for n in p]
                for rid, p in advisory.get("paths", {}).items()
            }
            for rid, goal in goals.items():
                req_path = assigned_paths.get(rid)
                navigate_to_node(
                    rid, goal, mode, **({"required_path": req_path} if req_path else {})
                )
        else:
            for rid, goal in goals.items():
                navigate_to_node(rid, goal, mode)


if __name__ == "__main__":
    unittest.main()
