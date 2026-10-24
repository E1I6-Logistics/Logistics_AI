"""정답 순서를 주지 않은 두 로봇 양보 탐색과 한도·확장 범위를 검증한다."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from backend.app.services.scenario_service import load_config
from backend.app.services.traffic_joint_actions import TrafficSituation
from backend.app.services.traffic_recovery_search import search_recovery_plan
from backend.simulation.traffic_joint_actions import JointActionSimulation, TestSelector as FakeSelector
from tests.test_fixed_traffic_plan import payload as fixed_payload


def search_payload():
    value = fixed_payload()
    options = next(s for s in load_config()["scenarios"] if s["name"] == "search_two_robot_yield")["traffic_joint_action"]
    del value["config"]["fixed_plan"]
    value["config"].update({k: deepcopy(options[k]) for k in ("yield_paths", "recovery_search")})
    return value


class RecoverySearchTests(unittest.TestCase):
    def test_finds_two_yields_without_fixed_steps_or_model(self):
        value = search_payload()
        self.assertNotIn("fixed_plan", value["config"])
        scene = TrafficSituation(value)
        selector = FakeSelector()
        decision = scene.decide(selector)
        self.assertEqual(selector.calls, 0)
        self.assertEqual(decision.selection_source, "CODE_SEARCH")
        self.assertEqual(scene.search_report["status"], "FOUND")
        self.assertEqual(scene.search_report["found_steps"], 5)
        self.assertEqual(scene.preview()["policy"]["selection_objective"], "FEWEST_STEPS_FIRST_WITHIN_LIMITS_NOT_MINIMUM_TIME")
        self.assertGreater(scene.search_report["checks"], 0)
        self.assertLessEqual(scene.search_report["checks"], 128)
        plan = scene.checked_plan(decision)
        self.assertEqual([s.action for s in plan.steps[:2]], ["YIELD", "YIELD"])
        self.assertEqual({s.robot_id for s in plan.steps[:2]}, {"robot2", "robot3"})
        self.assertEqual(plan.steps[2].robot_id, "robot1")
        result = JointActionSimulation(scene).execute(decision)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["steps_completed"], 5)
        self.assertEqual(result["capacity_violations"], 0)
        self.assertEqual(result["final_nodes"], {"robot1": "0", "robot2": "11", "robot3": "10"})

    def test_previous_single_yield_builder_cannot_resolve_same_input(self):
        value = search_payload()
        del value["config"]["recovery_search"]
        scene = TrafficSituation(value)
        self.assertEqual(set(scene.plans), {"HOLD_ALL"})
        self.assertEqual(len(scene.rejected), 3)

    def test_missing_yield_only_exhausts_supplied_actions(self):
        value = search_payload()
        del value["config"]["yield_paths"]["robot3"]
        scene = TrafficSituation(value)
        self.assertEqual(scene.search_report["status"], "EXHAUSTED")
        self.assertEqual(set(scene.plans), {"HOLD_ALL"})
        selector = FakeSelector()
        result = JointActionSimulation(scene).execute(scene.decide(selector))
        self.assertEqual(result["status"], "UNRESOLVED")
        self.assertEqual(result["steps_completed"], 0)
        self.assertEqual(selector.calls, 0)

    def test_step_and_check_limits_do_not_execute_partial_plan(self):
        for setting, expected in (({"max_steps": 4}, "STEP_LIMIT"), ({"max_checks": 1}, "CHECK_LIMIT")):
            with self.subTest(setting=setting):
                value = search_payload()
                value["config"]["recovery_search"].update(setting)
                scene = TrafficSituation(value)
                self.assertEqual(scene.search_report["status"], expected)
                self.assertEqual(set(scene.plans), {"HOLD_ALL"})
                result = JointActionSimulation(scene).execute(scene.decide())
                self.assertEqual(result["steps_completed"], 0)
                self.assertEqual(result["final_nodes"], {"robot1": "3", "robot2": "11", "robot3": "10"})

    def test_time_limit_discards_a_check_that_finished_late(self):
        value = search_payload()
        del value["config"]["recovery_search"]
        scene = TrafficSituation(value)
        now = [0.0]
        original = scene._preflight

        def slow_check(*args, **kwargs):
            result = original(*args, **kwargs)
            now[0] = 2.0
            return result

        with patch.object(scene, "_preflight", side_effect=slow_check):
            plan, report = search_recovery_plan(scene, {"max_seconds": 1.0}, clock=lambda: now[0])
        self.assertIsNone(plan)
        self.assertEqual(report["status"], "TIME_LIMIT")
        self.assertEqual(report["checks"], 1)

    def test_fourth_robot_and_renamed_ids_keep_search_generic(self):
        value = search_payload()
        extra = deepcopy(value["route_requests"][1])
        extra.update(current_node="4", node="4", result={"path": ["4"]})
        extra["order"]["robot_id"] = "robot4"
        value["route_requests"].append(extra)
        value["config"]["wait_allowed_nodes"].append("4")
        value["config"]["zone_occupancy"]["node:4"] = "robot4"
        # 기존 FMS의 robotN 형식을 유지하고 특정 번호·3대에 의존하지 않는지 본다.
        names = {"robot1": "robot41", "robot2": "robot22", "robot3": "robot8", "robot4": "robot99"}
        for request in value["route_requests"]:
            request["order"]["robot_id"] = names[request["order"]["robot_id"]]
        value["config"]["zone_occupancy"] = {k: names[v] for k, v in value["config"]["zone_occupancy"].items()}
        value["config"]["yield_paths"] = {names[k]: v for k, v in value["config"]["yield_paths"].items()}
        scene = TrafficSituation(value)
        result = JointActionSimulation(scene).execute(scene.decide())
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["final_nodes"]["robot99"], "4")
        self.assertTrue(all(s.robot_id != "robot99" for s in scene.plans["RECOVERY_BFS"].steps))

    def test_invalid_limits_and_mutually_exclusive_modes_are_rejected(self):
        for setting in ({"max_checks": True}, {"max_checks": 0}, {"max_steps": 33},
                        {"max_seconds": float("nan")}, {"max_seconds": 0}, {"unknown": 1}):
            with self.subTest(setting=setting):
                value = search_payload()
                value["config"]["recovery_search"] = setting
                with self.assertRaises(ValueError):
                    TrafficSituation(value)
        value = search_payload()
        value["config"]["fixed_plan"] = fixed_payload()["config"]["fixed_plan"]
        with self.assertRaisesRegex(ValueError, "동시에"):
            TrafficSituation(value)

    def test_blocked_physical_edge_and_forbidden_wait_node_are_respected(self):
        value = search_payload()
        value["config"]["blocked_edges"] = [["6", "11"]]
        scene = TrafficSituation(value)
        self.assertEqual(scene.search_report["status"], "EXHAUSTED")
        self.assertIn("BLOCKED_EDGE", scene.search_report["rejected_checks"])
        value = search_payload()
        value["config"]["wait_allowed_nodes"].remove("8")
        with self.assertRaisesRegex(ValueError, "대기 허용"):
            TrafficSituation(value)

    def test_prefix_permission_does_not_approve_an_incomplete_execution(self):
        scene = TrafficSituation(search_payload())
        plan = scene.plans["RECOVERY_BFS"]
        prefix = scene._plan("PARTIAL", "RECOVERY_SEARCH", plan.steps[:2])
        self.assertIsNone(scene._preflight(prefix, require_goal_restoration=False))
        self.assertEqual(scene._preflight(prefix), "ORIGINAL_GOAL_NOT_RESTORED")

    def test_return_from_workstation_requires_both_occupied_endpoints_to_clear(self):
        from backend.app.services.route_service import plan_route

        value = search_payload()
        self.assertEqual(value["route_requests"][0]["current_node"], "3")
        self.assertEqual(value["route_requests"][0]["node"], "0")
        for occupied in ("11", "10"):
            with self.subTest(occupied=occupied), self.assertRaises(ValueError):
                plan_route("3", "0", value["graph"], blocked_nodes=[occupied])


if __name__ == "__main__":
    unittest.main()
