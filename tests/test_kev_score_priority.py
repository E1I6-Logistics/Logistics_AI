"""A frozen KEV score order is independent of occupancy and reroute feasibility."""

from copy import deepcopy
from itertools import permutations
import unittest

from backend.app.services.kev_mapf.kev_adapter import plan_mapf_from_route_results
from tests.mock.kev_mock import MockKEV


def graph_for(paths, extra_edges=()):
    nodes = set(n for path in paths.values() for n in path)
    edges = {edge for path in paths.values() for edge in zip(path, path[1:])} | set(extra_edges)
    return {"type": "FeatureCollection", "features": [
        {"geometry": {"type": "Point", "coordinates": [float(n), float(n % 3)*2]},
         "properties": {"id": str(n)}} for n in sorted(nodes)
    ] + [
        {"geometry": {"type": "MultiLineString"},
         "properties": {"id": f"e{i}", "startid": str(a), "endid": str(b), "weight": 1.}}
        for i, (a, b) in enumerate(sorted(edges))
    ]}


def requests_for(paths):
    return [{"order": {"robot_id": rid, "selector": "kev", "model": "kev-08b"},
             "current_node": str(path[0]), "node": str(path[-1]),
             "result": {"path": list(map(str, path))}} for rid, path in paths.items()]


class Scores(MockKEV):
    def __init__(self, scores, *, reverse_zones=False, detours=None):
        self.scores, self.reverse_zones = scores, reverse_zones
        self.detours = detours or {}
        self.batches, self.states = [], []
        self.calls = self.forced = 0

    def score_many(self, state, robot_ids):
        self.batches.append(list(robot_ids))
        self.states.append(deepcopy(state))
        self.calls += 1
        return {rid: self.scores[rid] for rid in robot_ids}

    def pick(self, state, candidates, instructions, question_id):
        self.states.append(deepcopy(state))
        if self.reverse_zones and question_id.startswith("mapf_zone_"):
            return list(candidates)[-1]
        return super().pick(state, candidates, instructions, question_id)

    def route(self, graph_data, start, goal, **kwargs):
        if start not in self.detours:
            raise ValueError("no safe detour")
        return self.detours[start]


class ScorePriorityTests(unittest.TestCase):
    paths = {"robot1": [0, 3, 4, 7, 8], "robot2": [1, 3, 5, 7, 9], "robot3": [2, 3, 6, 7, 10]}
    scores = {"robot1": 90, "robot2": 70, "robot3": 50}
    expected = [["robot1", "robot2"], ["robot1", "robot3"], ["robot2", "robot3"]]

    def test_every_zone_and_input_order_uses_one_score_order(self):
        for order in permutations(self.paths):
            for reverse in (False, True):
                with self.subTest(order=order, reverse=reverse):
                    paths = {rid: self.paths[rid] for rid in order}
                    live = Scores(self.scores, reverse_zones=reverse)
                    result = plan_mapf_from_route_results(graph_for(paths), requests_for(paths), planner=live,
                                                          config={"wait_allowed_nodes": [0, 1, 2]})
                    self.assertEqual(result["status"], "PROPOSED")
                    self.assertEqual(result["global_priority_relations"], self.expected)
                    self.assertEqual({d["zone"] for d in result["decisions"]}, {"node:3", "node:7"})
                    for d in result["decisions"]:
                        self.assertGreater(self.scores[d["priority_robot"]], self.scores[d["yield_robot"]])
                    self.assertEqual(live.batches, [["robot1", "robot2", "robot3"]])
                    self.assertEqual(result["metrics"]["kev_score_calls"], 1)

    def test_tied_scores_use_robot_id_in_every_zone(self):
        for order in permutations(self.paths):
            paths = {rid: self.paths[rid] for rid in order}
            result = plan_mapf_from_route_results(graph_for(paths), requests_for(paths),
                                                  planner=Scores(dict.fromkeys(paths, 70)),
                                                  config={"wait_allowed_nodes": [0, 1, 2]})
            self.assertEqual(result["global_priority_relations"], self.expected)

    def test_no_wait_for_low_score_holds_instead_of_reversing_priority(self):
        paths = {"robot1": [0, 3, 4], "robot2": [1, 3, 5]}
        result = plan_mapf_from_route_results(graph_for(paths), requests_for(paths), planner=Scores(self.scores),
                                              config={"max_reroutes": 0, "wait_allowed_nodes": [0]})
        self.assertEqual(result["status"], "BLOCKED")
        d = result["decisions"][0]
        self.assertEqual((d["priority_robot"], d["yield_robot"], d["action"]), ("robot1", "robot2", "HOLD"))
        self.assertEqual(result["metrics"]["kev_score_calls"], 1)

    def test_parked_low_score_occupant_does_not_gain_priority_or_grant_entry(self):
        paths = {"robot1": [0, 3, 4], "robot2": [3]}
        result = plan_mapf_from_route_results(graph_for(paths), requests_for(paths), planner=Scores(self.scores),
                                              config={"zone_occupancy": {"node:3": "robot2"}, "wait_allowed_nodes": [0]})
        self.assertEqual(result["status"], "BLOCKED")
        d = result["decisions"][0]
        self.assertEqual((d["priority_robot"], d["yield_robot"], d["occupied_by"]), ("robot1", "robot2", "robot2"))
        self.assertEqual(d["priority_robot_action"], "HOLD")
        self.assertFalse(result["proceed_robots"])
        self.assertFalse(result["fms_execution_approved"])

    def test_occupied_reverse_edge_cannot_be_rerouted_mid_edge(self):
        paths = {"robot1": [0, 3, 4], "robot2": [3, 0, 5]}
        requests = requests_for(paths)
        requests[1].update(occupied_edge="e0", execution_committed=True)
        result = plan_mapf_from_route_results(graph_for(paths), requests, planner=Scores(self.scores),
                                              config={"zone_occupancy": {"edge:0:3": "robot2"}, "wait_allowed_nodes": [0, 3]})
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["decisions"][0]["priority_robot"], "robot1")
        self.assertEqual(result["decisions"][0]["action"], "HOLD")
        self.assertEqual(result["metrics"]["reroutes"], 0)
        self.assertEqual(result["required_paths"]["robot2"], ["3", "0", "5"])

    def test_active_low_score_cannot_wait_at_an_already_passed_upstream_node(self):
        paths = {"robot1": [0, 3, 4], "robot2": [1, 3, 5]}
        requests = requests_for(paths)
        requests[1].update(occupied_edge="e1", execution_committed=True)
        result = plan_mapf_from_route_results(graph_for(paths), requests, planner=Scores(self.scores),
                                              config={"wait_allowed_nodes": [0, 1]})
        self.assertEqual(result["status"], "BLOCKED")
        d = result["decisions"][0]
        self.assertEqual((d["priority_robot"], d["yield_robot"], d["action"]), ("robot1", "robot2", "HOLD"))
        self.assertEqual(d["priority_robot_action"], "HOLD")
        self.assertEqual(result["metrics"]["reroutes"], 0)
        self.assertFalse(result["global_priority_relations"])

    def test_occupant_can_take_validated_exit_without_reversing_score_order(self):
        paths = {"robot1": [0, 3, 4, 8], "robot2": [3, 4, 5]}
        live = Scores(self.scores, detours={3: [3, 6, 5]})
        result = plan_mapf_from_route_results(graph_for({**paths, "exit": [3, 6, 5]}), requests_for(paths), planner=live,
                                              config={"zone_occupancy": {"node:3": "robot2"}, "wait_allowed_nodes": [0]})
        d = result["decisions"][0]
        self.assertEqual(result["status"], "PROPOSED")
        self.assertEqual((d["priority_robot"], d["yield_robot"], d["action"]), ("robot1", "robot2", "REROUTE"))
        self.assertEqual(d["priority_robot_action"], "WAIT")
        self.assertEqual(result["required_paths"]["robot2"], ["3", "6", "5"])
        self.assertFalse(result["fms_execution_approved"])

    def test_reroute_checks_new_conflict_using_scores_of_initially_unrelated_robot(self):
        paths = {"robot1": [0, 3, 4], "robot2": [1, 3, 5], "robot3": [2, 6, 7]}
        live = Scores(self.scores, detours={1: [1, 6, 5]})
        result = plan_mapf_from_route_results(graph_for(paths, [(1, 6), (6, 5)]), requests_for(paths), planner=live,
                                              config={"max_reroutes": 1, "wait_allowed_nodes": [2]})
        self.assertEqual(result["status"], "PROPOSED")
        self.assertEqual([d["action"] for d in result["decisions"]], ["REROUTE", "WAIT"])
        self.assertEqual((result["decisions"][1]["priority_robot"], result["decisions"][1]["yield_robot"]), ("robot2", "robot3"))
        self.assertEqual(result["decisions"][1]["zone"], "node:6")
        self.assertEqual(live.batches, [["robot1", "robot2", "robot3"]])
        self.assertEqual(result["global_priority_relations"], [["robot2", "robot3"]])

    def test_invalid_score_set_fails_closed(self):
        paths = {"robot1": [0, 3, 4], "robot2": [1, 3, 5]}
        for scores in ({"robot1": 90}, {"robot1": 90, "robot2": 70, "extra": 50},
                       {"robot1": float("nan"), "robot2": 70}, {"robot1": float("inf"), "robot2": 70},
                       {"robot1": True, "robot2": 70}):
            with self.subTest(scores=scores):
                live = Scores(self.scores)
                live.score_many = lambda *_: scores
                with self.assertRaisesRegex(ValueError, "finite priority score"):
                    plan_mapf_from_route_results(graph_for(paths), requests_for(paths), planner=live)

    def test_eta_and_execution_schedule_do_not_enter_model_inputs(self):
        requests = requests_for(self.paths)
        for req in requests:
            req.update(eta_seconds=.1, expected_arrival_at=123, speed_mps=99, segment_departures=[10, 20])
            req["result"]["arrival_times"] = [10, 20, 30]
        live = Scores(self.scores)
        result = plan_mapf_from_route_results(graph_for(self.paths), requests, planner=live,
                                              config={"wait_allowed_nodes": [0, 1, 2]})
        forbidden = {"eta", "eta_seconds", "expected_arrival_at", "speed_mps", "segment_departures", "arrival_times"}
        def inspect(value):
            if isinstance(value, dict):
                self.assertFalse(forbidden.intersection(value))
                for item in value.values():
                    inspect(item)
            elif isinstance(value, list):
                for item in value:
                    inspect(item)
        for state in live.states:
            inspect(state)
            self.assertFalse(state["eta_available"])
        self.assertFalse(result["time_prediction_used"])
