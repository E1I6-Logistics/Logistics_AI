"""실제 모델 성능과 별개로 전략 비교의 공정성·입력 보존·검사 경계를 확인한다."""

from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from backend.app.config import BASE_DIR
from backend.app.services.traffic_joint_actions import fingerprint
from backend.simulation.traffic_strategy_comparison import CATALOG, build_input, compare_one, profile_limits
from backend.simulation.traffic_strategy_jhlee import advisory_plan, load_jhlee
from backend.simulation.traffic_strategy_search import (
    ComparisonLimits, LimitReached, PlanningBudget, PlanningScene, search_strategies,
)


class FirstStrategy:
    name = "test"

    def __init__(self):
        self.states = []

    def select_choice(self, state, criteria, instructions, **kwargs):
        self.states.append(deepcopy(state))
        return dict(choice=next(iter(criteria)))


class TrafficStrategyComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = load_jhlee()
        cls.catalog = json.loads(CATALOG.read_text())
        cls.graph = json.loads((Path(BASE_DIR) / cls.catalog["map"]).read_text())
        cls.limits = ComparisonLimits(**cls.catalog["limits"])
        cls.inputs = {spec["id"]: build_input(spec, cls.graph) for spec in cls.catalog["cases"]}

    def test_code_cases_execute_only_valid_complete_plans_and_preserve_input(self):
        for spec in self.catalog["cases"]:
            with self.subTest(case=spec["id"]):
                payload = self.inputs[spec["id"]]
                original = fingerprint(payload)
                row = compare_one(payload, "CODE_SEARCH", self.source, self.limits)
                self.assertEqual(row["solved"], spec["expected"] == "SOLVABLE")
                self.assertEqual(row["search"]["model_calls"], 0)
                self.assertEqual(row["input_sha256"], original)
                self.assertEqual(fingerprint(payload), original)
                if row["solved"]:
                    self.assertEqual(row["execution"]["capacity_violations"], 0)
                    self.assertTrue(row["execution"]["capacity_checks"] > 0)
                else:
                    self.assertIsNone(row["selected_plan"])
                    self.assertIsNone(row["execution"])
                    self.assertIsNone(row["planning_plus_estimated_mean_seconds"])

    def test_code_and_guided_same_order_share_moves_checks_routes_and_limits(self):
        payload = self.inputs["leader_leaves"]
        code = compare_one(payload, "CODE_SEARCH", self.source, self.limits)
        guided = compare_one(payload, "KEV_STRATEGY_SEARCH", self.source, self.limits, selector=FirstStrategy())
        self.assertEqual(code["selected_plan"], guided["selected_plan"])
        for key in ("limits", "attempts", "route_requests"):
            self.assertEqual(code["search"][key], guided["search"][key])
        self.assertEqual(guided["model_source"], "TEST_DOUBLE")
        self.assertEqual(guided["objective"], code["objective"])

    def test_model_proposes_strategy_before_detour_is_generated_and_receives_failures(self):
        payload = self.inputs["parked_blocker_detour"]
        scene, selector = PlanningScene(payload), FirstStrategy()
        budget = PlanningBudget(self.limits)
        with patch.object(budget, "route", wraps=budget.route) as route:
            original = selector.select_choice

            def choose(state, criteria, instructions, **kwargs):
                self.assertTrue(all("path" not in json.loads(value) for value in criteria.values()))
                if not selector.states:
                    self.assertEqual(route.call_count, 0)
                return original(state, criteria, instructions, **kwargs)

            selector.select_choice = choose
            plan, report = search_strategies(scene, budget, self.source.Graph, self.source.conflicts, selector=selector)
        self.assertIsNotNone(plan)
        self.assertTrue(any(entry["result"] == "NO_FEASIBLE_RESERVATION" for entry in report["strategy_feedback"]))
        self.assertTrue(report["route_calls"] > 0)
        self.assertEqual(plan.steps[0].action, "REROUTE")

    def test_guided_receives_failed_reservation_on_followup_and_no_prebuilt_plan(self):
        selector = FirstStrategy()
        row = compare_one(self.inputs["leader_leaves"], "KEV_STRATEGY_SEARCH", self.source,
                          self.limits, selector=selector)
        self.assertTrue(row["solved"])
        self.assertGreater(len(selector.states), 1)
        self.assertTrue(any(state["recent_checks"] for state in selector.states[1:]))
        self.assertTrue(all(state["planning_only"] for state in selector.states))

    def test_invalid_model_choice_and_model_error_cannot_approve_a_partial_plan(self):
        class Invalid:
            def select_choice(self, *args, **kwargs):
                return dict(choice="UNKNOWN")

        class Broken:
            def select_choice(self, *args, **kwargs):
                raise RuntimeError("시험 오류")

        for selector, reason in ((Invalid(), "INVALID_MODEL_CHOICE"), (Broken(), "MODEL_ERROR")):
            with self.subTest(reason=reason):
                row = compare_one(self.inputs["leader_leaves"], "KEV_STRATEGY_SEARCH", self.source,
                                  self.limits, selector=selector)
                self.assertEqual(row["search"]["stop_reason"], reason)
                self.assertIsNone(row["selected_plan"])
                self.assertIsNone(row["execution"])

    def test_last_allowed_check_can_find_plan_and_check_limit_never_executes_prefix(self):
        limit = replace(self.limits, max_checks=1)
        clear = compare_one(self.inputs["clear_pickup"], "CODE_SEARCH", self.source, limit)
        self.assertTrue(clear["solved"])
        blocked_prefix = compare_one(self.inputs["leader_leaves"], "CODE_SEARCH", self.source, limit)
        self.assertEqual(blocked_prefix["search"]["stop_reason"], "CHECK_LIMIT")
        self.assertIsNone(blocked_prefix["execution"])

    def test_model_budget_keeps_only_a_previously_validated_complete_plan(self):
        for calls, solved in ((1, False), (3, True)):
            with self.subTest(calls=calls):
                row = compare_one(self.inputs["three_departures"], "KEV_STRATEGY_SEARCH", self.source,
                                  replace(self.limits, max_model_calls=calls), selector=FirstStrategy())
                self.assertEqual(row["search"]["model_calls"], calls)
                self.assertEqual(row["search"]["stop_reason"], "MODEL_CALL_LIMIT")
                self.assertEqual(row["solved"], solved)

    def test_non_string_model_choice_is_rejected_without_execution(self):
        class InvalidType:
            def select_choice(self, *args, **kwargs):
                return dict(choice=[])

        row = compare_one(self.inputs["leader_leaves"], "KEV_STRATEGY_SEARCH", self.source,
                          self.limits, selector=InvalidType())
        self.assertEqual(row["search"]["stop_reason"], "INVALID_MODEL_CHOICE")
        self.assertIsNone(row["execution"])

    def test_late_preflight_result_is_not_used(self):
        now = [0.0]
        scene = PlanningScene(self.inputs["clear_pickup"])
        budget = PlanningBudget(self.limits, clock=lambda: now[0])

        def late_check(*args, **kwargs):
            now[0] = self.limits.max_seconds + 1
            return None

        with patch.object(scene, "_preflight", side_effect=late_check):
            plan, report = search_strategies(scene, budget, self.source.Graph, self.source.conflicts)
        self.assertEqual(report["stop_reason"], "TIME_LIMIT")
        self.assertIsNone(plan)

    def test_no_model_call_when_only_one_strategy_exists(self):
        selector = FirstStrategy()
        row = compare_one(self.inputs["clear_pickup"], "KEV_STRATEGY_SEARCH", self.source,
                          self.limits, selector=selector)
        self.assertTrue(row["solved"])
        self.assertEqual(selector.states, [])
        self.assertEqual(row["search"]["model_calls"], 0)

    def test_jhlee_original_engine_uses_common_routes_and_rejects_invalid_advisory(self):
        selector = FirstStrategy()
        row = compare_one(self.inputs["leader_leaves"], "JHLEE_STRATEGY", self.source,
                          self.limits, selector=selector)
        self.assertTrue(row["solved"])
        self.assertEqual(row["selected_plan"]["steps"][0]["robot_id"], "robot2")
        self.assertFalse(row["search"]["native_advisory"]["fms_execution_approved"])
        blocked = compare_one(self.inputs["parked_blocker_detour"], "JHLEE_STRATEGY", self.source,
                              self.limits, selector=FirstStrategy())
        self.assertFalse(blocked["solved"])
        self.assertIsNone(blocked["execution"])
        self.assertIn("PREFLIGHT_REJECTED", blocked["search"]["stop_reason"])

    def test_native_engine_routes_through_common_astar_without_live_route_generator(self):
        class ChooseReroute(FirstStrategy):
            def select_choice(self, state, criteria, instructions, **kwargs):
                return dict(choice="action_reroute" if "action_reroute" in criteria else next(iter(criteria)))

        row = compare_one(self.inputs["parked_blocker_detour"], "JHLEE_STRATEGY", self.source,
                          self.limits, selector=ChooseReroute())
        self.assertTrue(row["solved"])
        self.assertEqual(row["search"]["route_calls"], 1)
        self.assertEqual(row["selected_plan"]["steps"][0]["action"], "REROUTE")

    def test_native_advisory_requires_current_wait_node_and_acyclic_order(self):
        scene = PlanningScene(self.inputs["leader_leaves"])
        paths = {rid: list(r["path"]) for rid, r in scene.requests.items()}
        advisory = dict(status="PROPOSED", paths=paths,
                        global_priority_relations=[["robot1", "robot2"], ["robot2", "robot1"]], decisions=[])
        with self.assertRaisesRegex(ValueError, "PRIORITY_CYCLE"):
            advisory_plan(scene, advisory, max_steps=8)
        advisory["global_priority_relations"] = []
        advisory["decisions"] = [dict(action="WAIT", yield_robot="robot1", wait_node=12)]
        with self.assertRaisesRegex(ValueError, "NONCOMMON"):
            advisory_plan(scene, advisory, max_steps=8)

    def test_search_keeps_best_complete_plan_instead_of_stopping_at_first(self):
        scene = PlanningScene(self.inputs["three_departures"])
        plan, report = search_strategies(scene, PlanningBudget(self.limits), self.source.Graph, self.source.conflicts)
        self.assertIsNotNone(plan)
        self.assertGreater(report["valid_complete_plans"], 1)
        self.assertFalse(report["optimality_claim"])

    def test_limits_and_missing_selector_are_explicit(self):
        for values in (dict(max_checks=True), dict(max_model_calls=0), dict(max_seconds=float("nan")), dict(max_steps=33)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                ComparisonLimits(**values)
        with self.assertRaises(ValueError):
            compare_one(self.inputs["leader_leaves"], "KEV_STRATEGY_SEARCH", self.source, self.limits)

    def test_equal_time_profile_removes_only_total_model_call_cap(self):
        calls = asdict(profile_limits(self.catalog, "equal_calls"))
        timed = asdict(profile_limits(self.catalog, "equal_time"))
        self.assertEqual(calls.pop("max_model_calls"), 8)
        self.assertIsNone(timed.pop("max_model_calls"))
        self.assertEqual(calls, timed)
        with self.assertRaises(ValueError):
            profile_limits(self.catalog, "unknown")

    def test_no_total_call_cap_still_stops_at_time_limit(self):
        now = [0.0]
        budget = PlanningBudget(replace(self.limits, max_model_calls=None), clock=lambda: now[0])
        selector = FirstStrategy()
        for _ in range(10):
            self.assertEqual(budget.choose(selector, {}, {"A": "첫 전략", "B": "다른 전략"}, "선택", "probe"), "A")
        self.assertEqual(budget.model_calls, 10)
        now[0] = self.limits.max_seconds
        with self.assertRaisesRegex(LimitReached, "TIME_LIMIT"):
            budget.choose(selector, {}, {"A": "첫 전략", "B": "다른 전략"}, "선택", "probe")
        self.assertEqual(budget.model_calls, 10)

    def test_model_reports_keep_separate_limits_for_native_engine(self):
        row = compare_one(self.inputs["leader_leaves"], "JHLEE_STRATEGY", self.source,
                          replace(self.limits, max_model_calls=None), selector=FirstStrategy())
        self.assertTrue(row["solved"])
        self.assertIsNone(row["search"]["limits"]["max_model_calls"])
        self.assertEqual(row["search"]["native_engine_limits"], dict(rounds=16, max_reroutes=16))


if __name__ == "__main__":
    unittest.main()
