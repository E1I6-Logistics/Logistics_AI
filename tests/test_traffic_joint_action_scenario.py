"""기존 관제 시나리오 실행기와 실제 지도·Mock singleton의 연결을 검증한다."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from backend.app.services.mock_data import MockFmsStore
from backend.app.services.pathfinding import DistanceAStar
from backend.app.services.reservation import Reservation, ReservationTable, node_key
from backend.app.services.route_graph import load_route_graph
from backend.app.services.route_service import plan_route
from backend.app.services.scenario_service import ScenarioManager, load_config
from backend.simulation.scenarios import prepare
from backend.simulation.traffic_joint_action_scenario import run_joint_action_scenario
from backend.simulation.traffic_joint_actions import TestSelector
from tests.test_traffic_recovery_guidance import MoveSelector
from tests.test_traffic_recovery_deferred import PostponeSelector
from tests.test_traffic_recovery_dependencies import RuleSelector


class JointScenarioTests(unittest.TestCase):
    def test_ui_scenarios_drive_the_existing_mock_fleet(self):
        cases = [
            ("kev_joint_fms_input", choice, {"robot1": "11", "robot2": "8", "robot3": "0"}, operation, source)
            for operation in ("algorithm", "ai")
            for source in ("EXISTING_SHORTEST_PATH", "KEV")
            for choice in ("A1", "A2")
        ]
        fixed_cases = [
            ("dependency_code_two_robot_yield", "RECOVERY_DEPENDENCY_DFS", {"robot1": "0", "robot2": "11", "robot3": "10"}),
            ("dependency_kev_two_robot_yield", "RECOVERY_KEV_DEPENDENCY", {"robot1": "0", "robot2": "11", "robot3": "10"}),
            ("guided_deferred_two_robot_yield", "RECOVERY_KEV_DEPENDENCY", {"robot1": "0", "robot2": "11", "robot3": "10"}),
            ("guided_two_robot_yield", "RECOVERY_KEV", {"robot1": "0", "robot2": "11", "robot3": "10"}),
            ("dfs_two_robot_yield", "RECOVERY_DFS", {"robot1": "0", "robot2": "11", "robot3": "10"}),
            ("search_two_robot_yield", "RECOVERY_BFS", {"robot1": "0", "robot2": "11", "robot3": "10"}),
            ("fixed_two_robot_yield", "TWO_YIELDS", {"robot1": "0", "robot2": "11", "robot3": "10"}),
            ("kev_joint_wait_or_reroute", "A2", {"robot1": "11", "robot2": "5", "robot3": "0"}),
            ("kev_joint_wait_or_reroute", "B_REROUTE", {"robot1": "11", "robot2": "5", "robot3": "0"}),
            ("kev_joint_yield_or_reroute", "B_REROUTE", {"robot1": "11", "robot2": "7", "robot3": "0"}),
            ("kev_joint_yield_or_reroute", "B_YIELD_robot2", {"robot1": "11", "robot2": "7", "robot3": "0"}),
            ("kev_joint_proceed_wait", "A2", {"robot1": "4", "robot2": "3", "robot3": "6"}),
            ("kev_joint_reroute", "B_REROUTE", {"robot1": "11", "robot2": "7", "robot3": "0"}),
            ("kev_joint_yield", "B_YIELD_robot2", {"robot1": "11", "robot2": "7", "robot3": "0"}),
            ("kev_joint_hold", None, {"robot1": "9", "robot2": "7", "robot3": "0"}),
        ]
        cases.extend((name, choice, expected, operation, "SUPPLIED_PATHS")
                     for name, choice, expected in fixed_cases for operation in ("algorithm", "ai"))
        graph = load_route_graph()
        nodes = DistanceAStar(graph).nodes
        for name, selection, expected, operation, route_source in cases:
            with self.subTest(name=name, operation=operation, route_source=route_source, choice=selection):
                scenario = next(s for s in load_config()["scenarios"] if s["name"] == name)
                store = MockFmsStore(ReservationTable())
                manager = ScenarioManager()
                manager._status = dict(
                    state="running", mode="simulation", run_id=f"test-{name}",
                    name=name, message="", robots=[],
                )
                clock = [1.0]
                if name == "kev_joint_fms_input":
                    scenario["traffic_joint_action"]["route_source"] = route_source
                mode = SimpleNamespace(mode="simulation", operation_mode=operation)
                def generated(graph, current, goal, **kwargs):
                    self.assertEqual(kwargs, dict(selector="kev", model="kev-08b"))
                    return dict(path=plan_route(current, goal, graph)["node_ids"], calls=2)
                test_selector = MoveSelector(fail_first=True) if name == "guided_two_robot_yield" else TestSelector(selection)
                if name == "guided_deferred_two_robot_yield":
                    test_selector = PostponeSelector()
                if name == "dependency_kev_two_robot_yield":
                    test_selector = RuleSelector()
                with patch("backend.app.services.route_selector.kev_selector.KevSelector", side_effect=AssertionError("고정 시험에서 KEV 생성 금지")), patch("backend.app.services.mock_data.mock_fms", store), patch(
                    "backend.simulation.scenarios.mock_fms", store
                ), patch("backend.app.services.mock_data.monotonic", side_effect=lambda: clock[0]), patch(
                    "backend.app.services.mode_service.mode_manager", mode
                ), patch("backend.app.services.llm_route_preview.preview_llm_route", side_effect=generated) as route_preview:
                    prepare(scenario["robots"], nodes)
                    store._last_tick = clock[0]

                    async def run():
                        async def tick():
                            moving_ticks = 0
                            while manager._status["state"] == "running":
                                if not store._traffic.has_active_requests():
                                    await asyncio.sleep(0.001)
                                    continue
                                clock[0] += 0.05
                                store.advance_simulation(0.05, now=clock[0])
                                moving_ticks += 1
                                if moving_ticks > 5000:
                                    manager._status.update(
                                        state="stopping",
                                        message=f"테스트 가상시간 상한: {store._robots}",
                                    )
                                await asyncio.sleep(0)

                        task = asyncio.create_task(tick())
                        try:
                            await run_joint_action_scenario(
                                manager, scenario, graph, monotonic(), 30,
                                selector=None if name in ("dependency_code_two_robot_yield", "fixed_two_robot_yield", "search_two_robot_yield", "dfs_two_robot_yield") else test_selector, poll_interval=0,
                            )
                        finally:
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)

                    asyncio.run(run())
                    self.assertEqual(route_preview.call_count, 2 if route_source == "KEV" else 0)
                self.assertEqual(manager.status()["state"], "completed")
                self.assertEqual(
                    {rid: r["occupied_node"] for rid, r in store._robots.items()}, expected
                )
                selected = manager.status()["coordination"]["decision"]
                self.assertEqual(selected["joint_action_id"], selection or "HOLD_ALL")
                coordination = manager.status()["coordination"]
                if name in ("dependency_code_two_robot_yield", "dependency_kev_two_robot_yield"):
                    report = coordination["recovery_search"]
                    self.assertEqual(report["status"], "FOUND")
                    self.assertEqual(report["branch_policy"], "DEFER")
                    self.assertEqual(report["code_after_model_budget_decisions"], 0)
                    if name == "dependency_code_two_robot_yield":
                        self.assertEqual(report["model_calls"], 0)
                    else:
                        self.assertEqual(report["model_calls"], len(test_selector.states))
                        self.assertGreater(report["model_calls"], 0)
                if name == "guided_deferred_two_robot_yield":
                    self.assertEqual(selected["selection_source"], "HYBRID_TEST_CODE_SEARCH")
                    self.assertEqual(coordination["recovery_search"]["branch_policy"], "DEFER")
                    self.assertGreater(coordination["recovery_search"]["code_after_model_budget_decisions"], 0)
                    self.assertIn("코드 보완", manager.status()["message"])
                if name == "guided_two_robot_yield":
                    self.assertEqual(selected["selection_source"], "TEST_GUIDED_SEARCH")
                    self.assertEqual(coordination["traffic_selector"], "guided")
                    self.assertEqual(coordination["route_generation_calls"], 0)
                    self.assertEqual(coordination["joint_action_choice_requests"], 0)
                    self.assertGreater(coordination["recovery_guidance_requests"], 0)
                    self.assertEqual(coordination["recovery_guidance_requests"], len(test_selector.states))
                    self.assertEqual(coordination["recovery_search"]["status"], "FOUND")
                if name in ("fixed_two_robot_yield", "search_two_robot_yield"):
                    searching = name == "search_two_robot_yield"
                    self.assertEqual(selected["selection_source"], "CODE_SEARCH" if searching else "FIXED_SCENARIO")
                    self.assertEqual(coordination["traffic_selector"], "search" if searching else "fixed")
                    if searching:
                        self.assertEqual(coordination["recovery_search"]["status"], "FOUND")
                        self.assertNotIn("fixed_plan", scenario["traffic_joint_action"])
                    self.assertEqual(coordination["route_generation_calls"], 0)
                    self.assertEqual(coordination["joint_action_choice_requests"], 0)
                    self.assertEqual(test_selector.calls, 0)
                    expected_order = ["robot2", "robot3", "robot1", "robot2", "robot3"]
                    events = coordination["events"]
                    self.assertEqual([e["event"] for e in events], ["STEP_STARTED", "STEP_COMPLETED"] * 5)
                    self.assertEqual([e["robot_id"] for e in events[::2]], expected_order)
                    self.assertIn("KEV 호출 없음", manager.status()["message"])
                self.assertEqual(set(coordination["candidate_estimates"]), set(coordination["candidate_ids"]))
                actual = coordination["actual_metrics"]
                self.assertEqual(actual["clock"], "WALL_MONOTONIC")
                if name == "kev_joint_hold":
                    self.assertEqual(actual["outcome"], "UNRESOLVED")
                    self.assertIsNone(actual["mean_goal_completion_seconds"])
                    self.assertTrue(actual["unfinished_robot_ids"])
                else:
                    self.assertEqual(actual["outcome"], "COMPLETED")
                    self.assertFalse(actual["unfinished_robot_ids"])
                    self.assertIsNotNone(actual["mean_goal_completion_seconds"])
                    self.assertGreaterEqual(actual["mean_request_to_goal_seconds"], actual["mean_goal_completion_seconds"])
                self.assertTrue(all(r["route"] is None for r in store._robots.values()))
                if name in ("kev_joint_wait_or_reroute", "kev_joint_yield_or_reroute"):
                    coordination = manager.status()["coordination"]
                    expected_candidates = {"A2", "B_REROUTE"} if name == "kev_joint_wait_or_reroute" else {"B_REROUTE", "B_YIELD_robot2"}
                    self.assertEqual(set(coordination["candidate_ids"]), expected_candidates)
                    self.assertEqual(coordination["route_generation_calls"], 0)
                    self.assertEqual(coordination["joint_action_choice_requests"], 1)
                    label = scenario["traffic_joint_action"]["candidate_labels"][selection]
                    self.assertEqual(coordination["selected_description"], label)
                    self.assertIn(label, manager.status()["message"])
                    steps = coordination["selected_joint_action"]["steps"]
                    if selection == "B_REROUTE":
                        self.assertEqual(steps[0]["robot_id"], "robot1")
                        self.assertEqual(steps[0]["path"], ("9", "10", "8", "12", "11"))
                    elif selection == "A2":
                        self.assertEqual([s["robot_id"] for s in steps], ["robot2", "robot1"])
                    else:
                        self.assertEqual([s["robot_id"] for s in steps], ["robot2", "robot1", "robot2"])
                        self.assertEqual(steps[0]["action"], "YIELD")
                if name == "kev_joint_fms_input":
                    self.assertNotIn("paths", scenario["traffic_joint_action"])
                    coordination = manager.status()["coordination"]
                    self.assertEqual(coordination["input_source"], "ROUTE_RESULTS_AND_FMS_SNAPSHOT")
                    self.assertEqual(coordination["route_source"], route_source)
                    self.assertEqual(coordination["traffic_selector"], "kev")
                    self.assertEqual(coordination["route_generation_calls"], 4 if route_source == "KEV" else 0)
                    self.assertEqual(coordination["joint_action_choice_requests"], 1)
                    self.assertEqual(coordination["events"][0]["robot_id"], "robot1" if selection == "A1" else "robot2")

    def test_route_override_is_captured_per_run_without_changing_defaults(self):
        orders = SimpleNamespace(list_orders=lambda: [], returning={}, recovering={})
        for operation in ("ai", "algorithm"):
            for source in (None, "EXISTING_SHORTEST_PATH", "KEV"):
                with self.subTest(operation=operation, source=source):
                    manager = ScenarioManager()
                    store = MockFmsStore(ReservationTable())
                    mode = SimpleNamespace(mode="simulation", operation_mode=operation)
                    with patch("backend.app.services.scenario_service.mode_manager", mode), patch(
                        "backend.app.services.order_manager.order_manager", orders
                    ), patch("backend.app.services.mock_data.mock_fms", store), patch.object(
                        manager, "_run", new_callable=AsyncMock
                    ) as run:
                        async def start():
                            status = manager.start("kev_joint_fms_input", route_source=source)
                            await manager._task
                            return status

                        status = asyncio.run(start())
                    expected = source or "EXISTING_SHORTEST_PATH"
                    self.assertEqual(status["route_source"], expected)
                    self.assertEqual(status["traffic_selector"], "kev")
                    self.assertEqual(run.call_args.args[0]["traffic_joint_action"]["route_source"], expected)
                    default = next(s for s in load_config()["scenarios"] if s["name"] == "kev_joint_fms_input")
                    self.assertEqual(default["traffic_joint_action"]["route_source"], "EXISTING_SHORTEST_PATH")

    def test_route_override_rejects_unrelated_and_fixed_scenarios_before_placement(self):
        orders = SimpleNamespace(list_orders=lambda: [], returning={}, recovering={})
        for name, source in (("bottleneck", "KEV"), ("continuous_ai_orders", "KEV"),
                             ("fixed_two_robot_yield", "KEV"),
                             ("kev_joint_hold", "EXISTING_SHORTEST_PATH"), ("kev_joint_fms_input", "invalid")):
            with self.subTest(name=name, source=source), patch(
                "backend.app.services.order_manager.order_manager", orders
            ), patch("backend.simulation.scenarios.prepare") as placement:
                with self.assertRaisesRegex(ValueError, "선택"):
                    ScenarioManager().start(name, route_source=source)
                placement.assert_not_called()

    def test_scenario_api_exposes_default_and_passes_the_selected_source(self):
        from backend.app.routers.scenarios import RunScenario, list_scenarios, run_scenario

        items = asyncio.run(list_scenarios())
        item = next(s for s in items if s["name"] == "kev_joint_fms_input")
        self.assertEqual(item["route_source"], "EXISTING_SHORTEST_PATH")
        self.assertEqual(item["route_source_options"], ["EXISTING_SHORTEST_PATH", "KEV"])
        self.assertEqual(item["traffic_selector"], "kev")
        self.assertEqual(item["group"], "경로 연결 확인")
        for name in ("kev_joint_wait_or_reroute", "kev_joint_yield_or_reroute"):
            compare = next(s for s in items if s["name"] == name)
            self.assertEqual(compare["group"], "교통조정만 시험")
            self.assertTrue(compare["test_goal"])
        ordinary = next(s for s in items if s["name"] == "bottleneck")
        fixed = next(s for s in items if s["name"] == "fixed_two_robot_yield")
        self.assertEqual(fixed["traffic_selector"], "fixed")
        self.assertEqual(fixed["route_source_options"], [])
        self.assertIn("KEV", fixed["test_goal"])
        search = next(s for s in items if s["name"] == "search_two_robot_yield")
        self.assertEqual(search["traffic_selector"], "search")
        self.assertEqual(search["route_source_options"], [])
        guided = next(s for s in items if s["name"] == "guided_two_robot_yield")
        self.assertEqual(guided["traffic_selector"], "guided")
        self.assertEqual(guided["group"], "개발용 보관")
        self.assertNotIn("route_source_options", ordinary)
        with patch("backend.app.routers.scenarios.scenario_manager") as manager:
            manager.start.return_value = {"state": "running"}
            asyncio.run(run_scenario(RunScenario(name=item["name"], route_source="KEV")))
            manager.start.assert_called_once_with(item["name"], route_source="KEV")

    def test_ui_scenario_rejects_real_mode_before_initial_placement(self):
        manager = ScenarioManager()
        orders = SimpleNamespace(list_orders=lambda: [], returning={}, recovering={})
        for name in ("guided_two_robot_yield", "dfs_two_robot_yield", "search_two_robot_yield", "fixed_two_robot_yield", "kev_joint_proceed_wait", "kev_joint_wait_or_reroute", "kev_joint_yield_or_reroute"):
            with self.subTest(name=name), patch("backend.app.services.scenario_service.mode_manager", SimpleNamespace(mode="real")), patch(
                "backend.app.services.order_manager.order_manager", orders
            ), patch("backend.simulation.scenarios.prepare") as placement:
                with self.assertRaisesRegex(ValueError, "시뮬레이션"):
                    manager.start(name)
                placement.assert_not_called()

    def test_fixed_path_scenarios_start_in_ai_without_changing_their_source(self):
        orders = SimpleNamespace(list_orders=lambda: [], returning={}, recovering={})
        for name in ("kev_joint_proceed_wait", "kev_joint_reroute", "kev_joint_yield", "kev_joint_hold", "kev_joint_wait_or_reroute", "kev_joint_yield_or_reroute"):
            manager = ScenarioManager()
            store = MockFmsStore(ReservationTable())
            with self.subTest(name=name), patch(
                "backend.app.services.scenario_service.mode_manager", SimpleNamespace(mode="simulation", operation_mode="ai")
            ), patch("backend.app.services.order_manager.order_manager", orders), patch(
                "backend.app.services.mock_data.mock_fms", store
            ), patch.object(manager, "_run", new_callable=AsyncMock) as run:
                async def start():
                    status = manager.start(name)
                    await manager._task
                    return status

                status = asyncio.run(start())
                self.assertEqual(status["operation_mode"], "ai")
                self.assertEqual(status["route_source"], "SUPPLIED_PATHS")
                self.assertEqual(status["traffic_selector"], "kev")
                self.assertIn("paths", run.call_args.args[0]["traffic_joint_action"])

    def test_normal_ai_scenario_generates_all_routes_before_navigation(self):
        scenario = next(s for s in load_config()["scenarios"] if s["name"] == "bottleneck")
        graph = load_route_graph()
        for fail in (False, True):
            with self.subTest(fail=fail):
                store = MockFmsStore(ReservationTable())
                manager = ScenarioManager()
                manager._status = dict(state="running", mode="simulation", operation_mode="ai", run_id="ai-normal", message="")
                mode = SimpleNamespace(mode="simulation", operation_mode="ai")
                events = []

                def generate(graph, start, goal, **kwargs):
                    events.append("route")
                    if fail and len(events) == 2:
                        raise ValueError("KEV 경로 생성 실패")
                    self.assertEqual(kwargs, dict(selector="kev", model="kev-08b"))
                    return dict(path=plan_route(start, goal, graph)["node_ids"], calls=2)

                def arrive(rid, goal, mode, *, required_path=None):
                    events.append("navigate")
                    expected = plan_route(store._robots[rid]["occupied_node"], goal, graph)["node_ids"]
                    self.assertEqual(required_path, expected)
                    store._robots[rid].update(current_node=goal, occupied_node=goal, status="IDLE", goal_node=None, route=None)

                with patch("backend.app.services.mock_data.mock_fms", store), patch(
                    "backend.simulation.scenarios.mock_fms", store
                ), patch("backend.app.services.scenario_service.mode_manager", mode), patch(
                    "backend.app.services.llm_route_preview.preview_llm_route", side_effect=generate
                ), patch("backend.app.services.scenario_service.navigate_to_node", side_effect=arrive) as navigate:
                    asyncio.run(manager._run(scenario, graph, DistanceAStar(graph).nodes, 30))
                self.assertEqual(manager.status()["state"], "failed" if fail else "completed")
                if fail:
                    navigate.assert_not_called()
                    self.assertIn("KEV 경로 생성 실패", manager.status()["message"])
                else:
                    self.assertEqual(events, ["route"]*3 + ["navigate"]*3)
                    self.assertEqual(manager.status()["route_generation"]["source"], "KEV")

    def test_ai_route_failure_in_joint_scenario_does_not_fall_back_or_move(self):
        graph = load_route_graph()
        scenario = next(s for s in load_config()["scenarios"] if s["name"] == "kev_joint_fms_input")
        scenario["traffic_joint_action"]["route_source"] = "KEV"
        store = MockFmsStore(ReservationTable())
        manager = ScenarioManager()
        manager._status = dict(state="running", mode="simulation", run_id="ai-route-failure", message="")
        with patch("backend.app.services.mock_data.mock_fms", store), patch(
            "backend.simulation.scenarios.mock_fms", store
        ), patch("backend.app.services.mode_service.mode_manager", SimpleNamespace(mode="simulation", operation_mode="ai")):
            prepare(scenario["robots"], DistanceAStar(graph).nodes)
            with patch("backend.app.services.llm_route_preview.preview_llm_route", side_effect=ValueError("모델 경로 실패")), patch.object(
                store, "navigate_to_node"
            ) as navigate, patch("backend.app.services.route_service.plan_route") as astar:
                with self.assertRaisesRegex(ValueError, "모델 경로 실패"):
                    asyncio.run(run_joint_action_scenario(manager, scenario, graph, monotonic(), 30, selector=TestSelector()))
                navigate.assert_not_called()
                astar.assert_not_called()

    def test_existing_resources_are_rejected_before_placement_can_clear_them(self):
        for protection in (False, True):
            with self.subTest(protection=protection):
                store = MockFmsStore(ReservationTable())
                if protection:
                    store._traffic._protected_resources["robot1"] = {node_key("7")}
                else:
                    store._traffic._reservations.try_commit("robot1", "existing", [
                        Reservation(node_key("7"), "robot1", "existing", 0, 0, 10)
                    ])
                reserved_before = store._traffic._reservations.snapshot()
                protected_before = deepcopy(store._traffic._protected_resources)
                orders = SimpleNamespace(list_orders=lambda: [], returning={}, recovering={})
                with patch("backend.app.services.scenario_service.mode_manager", SimpleNamespace(mode="simulation", operation_mode="algorithm")), patch(
                    "backend.app.services.order_manager.order_manager", orders
                ), patch("backend.app.services.mock_data.mock_fms", store), patch(
                    "backend.simulation.scenarios.prepare"
                ) as placement:
                    with self.assertRaisesRegex(ValueError, "활성 예약/보호 자원"):
                        ScenarioManager().start("kev_joint_fms_input")
                    placement.assert_not_called()
                self.assertEqual(store._traffic._reservations.snapshot(), reserved_before)
                self.assertEqual(store._traffic._protected_resources, protected_before)

    def test_stop_during_model_selection_prevents_navigation(self):
        graph = load_route_graph()
        scenario = next(s for s in load_config()["scenarios"] if s["name"] == "kev_joint_proceed_wait")
        store = MockFmsStore(ReservationTable())
        manager = ScenarioManager()
        manager._status = dict(
            state="running", mode="simulation", run_id="test-stopping", message="", robots=[]
        )

        class StoppingSelector(TestSelector):
            def select_choice(self, *args, **kwargs):
                manager._status.update(state="stopping", message="정지 요청")
                return {"choice": "A1"}

        with patch("backend.app.services.mock_data.mock_fms", store), patch(
            "backend.simulation.scenarios.mock_fms", store
        ), patch("backend.app.services.mode_service.mode_manager", SimpleNamespace(mode="simulation", operation_mode="algorithm")):
            prepare(scenario["robots"], DistanceAStar(graph).nodes)
            before = deepcopy(store._robots)
            with patch.object(store, "navigate_to_node", wraps=store.navigate_to_node) as navigate:
                with self.assertRaisesRegex(ValueError, "정지 요청"):
                    asyncio.run(run_joint_action_scenario(
                        manager, scenario, graph, monotonic(), 30, selector=StoppingSelector()
                    ))
                navigate.assert_not_called()
            self.assertEqual(store._robots, before)

    def test_interrupted_movement_is_logged_as_unfinished_not_success(self):
        graph = load_route_graph()
        scenario = next(s for s in load_config()["scenarios"] if s["name"] == "kev_joint_proceed_wait")
        store = MockFmsStore(ReservationTable())
        manager = ScenarioManager()
        manager._status = dict(state="running", mode="simulation", run_id="interrupted-metrics", message="", robots=[])
        with TemporaryDirectory() as folder, patch("backend.app.config.BASE_DIR", Path(folder)), patch(
            "backend.app.services.mock_data.mock_fms", store
        ), patch("backend.simulation.scenarios.mock_fms", store), patch(
            "backend.app.services.mode_service.mode_manager", SimpleNamespace(mode="simulation", operation_mode="ai")
        ):
            prepare(scenario["robots"], DistanceAStar(graph).nodes)

            def interrupted(*args, **kwargs):
                manager._status.update(state="stopping", message="시험 중단")

            with patch.object(store, "navigate_to_node", side_effect=interrupted):
                with self.assertRaisesRegex(ValueError, "시험 중단"):
                    asyncio.run(run_joint_action_scenario(
                        manager, scenario, graph, monotonic(), 30, selector=TestSelector("A1")
                    ))
            report = json.loads((Path(folder) / "logs/traffic-joint-actions/run-interrupted-metrics.json").read_text())
            self.assertEqual(report["state"], "failed")
            self.assertIn("traffic_input", report)
            actual = report["coordination"]["actual_metrics"]
            self.assertEqual(actual["outcome"], "INTERRUPTED")
            self.assertEqual(actual["completed_request_count"], 0)
            self.assertIsNone(actual["mean_goal_completion_seconds"])
            self.assertEqual(len(actual["unfinished_robot_ids"]), 3)
            self.assertIsNone(actual["steps"][0]["completed_seconds"])

    def test_stop_during_code_search_prevents_navigation(self):
        from backend.app.services.traffic_joint_actions import TrafficSituation

        graph = load_route_graph()
        scenario = next(s for s in load_config()["scenarios"] if s["name"] == "search_two_robot_yield")
        store = MockFmsStore(ReservationTable())
        manager = ScenarioManager()
        manager._status = dict(state="running", mode="simulation", run_id="stop-search", message="", robots=[])

        def interrupted_search(payload, **kwargs):
            manager._status.update(state="stopping", message="탐색 중 정지 요청")
            return TrafficSituation(payload, **kwargs)

        with TemporaryDirectory() as folder, patch("backend.app.config.BASE_DIR", Path(folder)), patch(
            "backend.app.services.mock_data.mock_fms", store
        ), patch("backend.simulation.scenarios.mock_fms", store), patch(
            "backend.app.services.mode_service.mode_manager", SimpleNamespace(mode="simulation", operation_mode="ai")
        ):
            prepare(scenario["robots"], DistanceAStar(graph).nodes)
            with patch("backend.simulation.traffic_joint_action_scenario.TrafficSituation", side_effect=interrupted_search), patch.object(
                store, "navigate_to_node"
            ) as navigate:
                with self.assertRaisesRegex(ValueError, "탐색 중 정지 요청"):
                    asyncio.run(run_joint_action_scenario(manager, scenario, graph, monotonic(), 30))
                navigate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
