"""같은 상태의 확률 재사용이 검사·한도·피드백 경계를 지키는지 확인한다."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from backend.app.config import BASE_DIR
from backend.simulation.traffic_strategy_comparison import CATALOG, build_input, compare_one
from backend.simulation.traffic_strategy_jhlee import load_jhlee
from backend.simulation.traffic_strategy_search import ComparisonLimits, LimitReached, PlanningBudget, PlanningScene, search_strategies


class OrderedProbabilities:
    name = "test"

    def __init__(self):
        self.states = []

    def select_choice(self, state, criteria, instructions, **kwargs):
        self.states.append(deepcopy(state))
        keys = list(criteria)
        weights = list(range(len(keys), 0, -1))
        return dict(choice=keys[0], probabilities={k: w / sum(weights) for k, w in zip(keys, weights)})


class ProbabilityOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = load_jhlee()
        cls.catalog = json.loads(CATALOG.read_text())
        graph = json.loads((Path(BASE_DIR) / cls.catalog["map"]).read_text())
        cls.limits = replace(ComparisonLimits(**cls.catalog["limits"]), max_model_calls=None)
        cls.inputs = {spec["id"]: build_input(spec, graph) for spec in cls.catalog["cases"]}

    def test_same_order_preserves_paths_checks_input_and_execution_for_all_cases(self):
        for spec in self.catalog["cases"]:
            with self.subTest(case=spec["id"]):
                payload = self.inputs[spec["id"]]
                original = deepcopy(payload)
                code = compare_one(payload, "CODE_SEARCH", self.source, self.limits)
                ranked = compare_one(payload, "KEV_PROBABILITY_ORDER", self.source, self.limits,
                                     selector=OrderedProbabilities())
                self.assertEqual(payload, original)
                self.assertEqual(code["selected_plan"], ranked["selected_plan"])
                for key in ("limits", "attempts", "route_requests"):
                    self.assertEqual(code["search"][key], ranked["search"][key])
                self.assertEqual(ranked["solved"], spec["expected"] == "SOLVABLE")
                if ranked["solved"]:
                    self.assertEqual(ranked["execution"]["capacity_violations"], 0)
                else:
                    self.assertIsNone(ranked["execution"])

    def test_reuses_root_ranking_after_failures_but_new_prefix_receives_feedback(self):
        selector = OrderedProbabilities()
        ranked = compare_one(self.inputs["three_departures"], "KEV_PROBABILITY_ORDER", self.source,
                             self.limits, selector=selector)
        repeated = compare_one(self.inputs["three_departures"], "KEV_STRATEGY_SEARCH", self.source,
                               self.limits, selector=OrderedProbabilities())
        self.assertLess(ranked["search"]["model_calls"], repeated["search"]["model_calls"])
        prefixes = [json.dumps(s["partial_steps"], sort_keys=True) for s in selector.states]
        self.assertEqual(len(prefixes), len(set(prefixes)))
        self.assertEqual(sum(not s["partial_steps"] for s in selector.states), 1)
        self.assertTrue(any(s["recent_checks"] for s in selector.states[1:]))
        self.assertTrue(any(s["partial_steps"] for s in selector.states[1:]))
        self.assertTrue(ranked["search"]["probability_rankings"])

    def test_invalid_probabilities_stop_without_inventing_order(self):
        bad = ({}, {"A": 1}, {"A": 0.5, "B": 0.5, "C": 0},
               {"A": float("nan"), "B": 1}, {"A": -0.1, "B": 1.1},
               {"A": True, "B": 0}, {"A": "0.5", "B": 0.5}, {"A": 0, "B": 0})
        for probabilities in bad:
            with self.subTest(probabilities=probabilities):
                selector = OrderedProbabilities()
                selector.select_choice = lambda *a, **k: dict(choice="A", probabilities=probabilities)
                budget = PlanningBudget(self.limits)
                with self.assertRaisesRegex(LimitReached, "INVALID_MODEL_PROBABILITIES"):
                    budget.rank(selector, {}, {"A": "a", "B": "b"}, "select", "probe")
                self.assertEqual(budget.model_calls, 1)
                self.assertEqual(budget.calls[-1]["status"], "INVALID_MODEL_PROBABILITIES")

    def test_probability_order_and_ties_are_not_the_input_choice(self):
        selector = OrderedProbabilities()
        selector.select_choice = lambda *a, **k: dict(choice="C", probabilities={"A": .2, "B": .4, "C": .4})
        order = PlanningBudget(self.limits).rank(selector, {}, {"A": "a", "B": "b", "C": "c"}, "select", "probe")
        self.assertEqual(order, ["B", "C", "A"])

    def test_single_option_skips_server_and_partial_plan_is_not_executed_at_call_limit(self):
        selector = OrderedProbabilities()
        clear = compare_one(self.inputs["clear_pickup"], "KEV_PROBABILITY_ORDER", self.source,
                            self.limits, selector=selector)
        self.assertTrue(clear["solved"])
        self.assertEqual(selector.states, [])
        row = compare_one(self.inputs["three_departures"], "KEV_PROBABILITY_ORDER", self.source,
                          replace(self.limits, max_model_calls=1), selector=selector)
        self.assertEqual(row["search"]["stop_reason"], "MODEL_CALL_LIMIT")
        self.assertEqual(row["search"]["model_calls"], 1)
        self.assertIsNone(row["execution"])
        completed = compare_one(self.inputs["three_departures"], "KEV_PROBABILITY_ORDER", self.source,
                                replace(self.limits, max_model_calls=2), selector=OrderedProbabilities())
        self.assertEqual(completed["search"]["stop_reason"], "MODEL_CALL_LIMIT")
        self.assertTrue(completed["solved"])
        self.assertEqual(completed["execution"]["capacity_violations"], 0)

    def test_cached_order_cannot_bypass_time_or_check_limit(self):
        scene = PlanningScene(self.inputs["leader_leaves"])
        now = [0.0]
        budget = PlanningBudget(self.limits, clock=lambda: now[0])

        def late(*args, **kwargs):
            now[0] = self.limits.max_seconds + 1
            return "NO_FEASIBLE_RESERVATION"

        with patch.object(scene, "_preflight", side_effect=late):
            plan, report = search_strategies(scene, budget, self.source.Graph, self.source.conflicts,
                                             selector=OrderedProbabilities(), probability_order=True)
        self.assertIsNone(plan)
        self.assertEqual(report["stop_reason"], "TIME_LIMIT")
        limited = compare_one(self.inputs["leader_leaves"], "KEV_PROBABILITY_ORDER", self.source,
                              replace(self.limits, max_checks=1), selector=OrderedProbabilities())
        self.assertEqual(limited["search"]["stop_reason"], "CHECK_LIMIT")
        self.assertIsNone(limited["execution"])


if __name__ == "__main__":
    unittest.main()
