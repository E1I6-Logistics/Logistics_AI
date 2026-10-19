"""MAPF boundary tests; no model server, ROS or FMS singleton is called."""
import json
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from backend.app.services import kev_mapf_standalone as mapf
from backend.app.services import route_service


def graph_data():
    points = {1: (0, 0), 2: (1, 0), 3: (2, 0), 4: (3, 0), 5: (0, 1), 6: (2, 1), 7: (3, 1)}
    edges = [(1, 2, 0), (2, 3, 8), (3, 4, 2), (5, 3, 5), (3, 7, 3),
             (5, 6, 9), (6, 7, 1), (1, 4, 1), (2, 1, 1)]
    return {"type": "FeatureCollection", "features": [
        {"geometry": {"type": "Point", "coordinates": list(xy)}, "properties": {"id": str(n)}}
        for n, xy in points.items()
    ] + [
        {"geometry": {"type": "MultiLineString"},
         "properties": {"id": str(i), "startid": str(a), "endid": str(b), "weight": w}}
        for i, (a, b, w) in enumerate(edges)
    ]}


def requests():
    return [
        {"order": {"robot_id": "a", "selector": "kev", "model": "kev-08b"},
         "current_node": "1", "node": "4", "result": {"path": ["1", "2", "3", "4"]}},
        {"order": {"robot_id": "b", "selector": "ollama", "model": "chosen-model"},
         "current_node": "5", "node": "7", "result": {"path": ["5", "3", "7"]},
         "status": "WAITING", "task_priority": 7},
    ]


class Decisions(mapf.MockKEV):
    def __init__(self, action="action_wait"):
        self.action = action
        self.states = []
        self.route = Mock(side_effect=AssertionError("unexpected new route inference"))

    def pick(self, state, candidates, instructions, question_id):
        self.states.append(deepcopy(state))
        if question_id.startswith("action_"):
            return self.action
        return next(iter(candidates))


class MapfIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.graph = graph_data()
        self.requests = requests()

    def plan(self, **kwargs):
        return mapf.plan_mapf_from_route_results(self.graph, self.requests, **kwargs)

    def test_provided_non_shortest_paths_are_preserved_without_inference(self):
        planner = Decisions()
        before = deepcopy((self.graph, self.requests))
        result = self.plan(planner=planner, config={"wait_allowed_nodes": [5]})
        self.assertEqual(result["required_paths"], {"a": ["1", "2", "3", "4"], "b": ["5", "3", "7"]})
        self.assertEqual(result["paths"]["a"], [1, 2, 3, 4])
        self.assertEqual(result["path_sources"], {"a": "preview_result", "b": "preview_result"})
        self.assertFalse(result["fms_execution_approved"])
        self.assertFalse(result["time_prediction_used"])
        planner.route.assert_not_called()
        self.assertEqual((self.graph, self.requests), before)

    def test_every_decision_receives_full_graph_and_robot_neighbors(self):
        planner = Decisions()
        self.plan(planner=planner, config={"wait_allowed_nodes": [5]})
        self.assertEqual(len(planner.states), 4)  # zone, priority, action, wait
        for state in planner.states:
            json.dumps(state)
            self.assertEqual(state["route_graph"]["nodes"], list(range(1, 8)))
            self.assertTrue(state["route_graph"]["directed"])
            robots = {r["id"]: r for r in state["robots"]}
            self.assertEqual(robots["a"]["available_edges"], [{"node": 2, "weight": 0}, {"node": 4, "weight": 1}])
            self.assertEqual(robots["b"]["status"], "WAITING")
            self.assertEqual(robots["b"]["task_priority"], 7)

    def test_graph_weights_and_validation_match_route_service(self):
        points, edges = route_service.build_route_inputs(self.graph, require_stored_weight=True)
        g = mapf.Graph(self.graph)
        self.assertEqual(g.nodes, points)
        self.assertEqual(g.edges, route_service.build_edge_weight_lookup(points, edges))
        with patch.object(route_service, "validate_and_calculate_path_distance", wraps=route_service.validate_and_calculate_path_distance) as validate:
            g.check_path([1, 2, 3, 4], start=1, goal=4)
            validate.assert_called_once()
        self.graph["features"][-1]["properties"].pop("weight")
        with self.assertRaisesRegex(ValueError, "weight"):
            mapf.Graph(self.graph)

    def test_invalid_endpoints_nodes_and_reverse_edges_are_rejected(self):
        for path in (["2", "3", "4"], ["1", "2"], ["1", "99", "4"], ["1", "3", "4"], []):
            with self.subTest(path=path):
                self.requests[0]["result"]["path"] = path
                with self.assertRaises(ValueError):
                    self.plan(planner=Decisions())

    def test_reused_directed_edge_is_rejected(self):
        self.requests[0]["result"]["path"] = ["1", "2", "1", "2", "3", "4"]
        with self.assertRaisesRegex(ValueError, "reuse"):
            self.plan(planner=Decisions())

    def test_duplicate_robot_and_missing_result_are_rejected(self):
        self.requests[1]["order"]["robot_id"] = "a"
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.plan(planner=Decisions())
        self.requests = requests()
        del self.requests[1]["result"]
        with self.assertRaises(KeyError):
            self.plan(planner=Decisions())

    def test_single_robot_and_idle_node_occupant(self):
        result = mapf.plan_mapf_from_route_results(self.graph, self.requests[:1], planner=Decisions())
        self.assertEqual(result["decisions"], [])
        self.requests[1].update(current_node="3", node="3", result={"path": ["3"]})
        planner = Decisions(action="action_hold")
        result = self.plan(planner=planner, config={"zone_occupancy": {"node:3": "b"}})
        self.assertEqual(result["decisions"][0]["priority_robot"], "b")
        self.assertEqual(result["status"], "BLOCKED")

    def test_node_ids_reject_lossy_conversion(self):
        for value in (True, 1.9, "abc"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                mapf.mapf_robot_from_route_result("a", value, "4", {"path": ["1", "4"]})

    def test_reroute_reuses_preview_with_original_robot_model(self):
        live = mapf.LiveKEV(Path(__file__).resolve().parents[1], model="kev-08b", deadline=0.15, max_steps=None)
        live.preview_llm_route = Mock(return_value={"path": ["5", "6", "7"], "calls": 1})
        planner = Decisions(action="action_reroute")
        planner.route = live.route
        before = deepcopy((self.graph, self.requests))
        result = self.plan(planner=planner)
        args, kwargs = live.preview_llm_route.call_args
        self.assertEqual(args[1:], ("5", "7"))
        self.assertEqual(
            kwargs,
            {
                "selector": "ollama",
                "model": "chosen-model",
                "traffic": {"occupied_nodes": {1: "a"}, "robot_paths": {"a": [1, 2, 3, 4]}},
            },
        )
        self.assertNotIn(3, mapf.Graph(args[0]).nodes)
        self.assertEqual(result["required_paths"]["b"], ["5", "6", "7"])
        self.assertEqual((self.graph, self.requests), before)

    def test_missing_catalog_propagates_without_runner_fallback(self):
        live = mapf.LiveKEV(Path(__file__).resolve().parents[1], model="kev-08b", deadline=0.15, max_steps=None)
        live.preview_llm_route = Mock(side_effect=FileNotFoundError("selector_iterative_benchmark.json"))
        with self.assertRaises(FileNotFoundError):
            live.route(self.graph, 1, 4)
        live.preview_llm_route.assert_called_once()
        planner = Decisions(action="action_reroute")
        planner.route = live.route
        result = self.plan(planner=planner)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["decisions"][0]["reason"], "REROUTE_FAILED")
        self.assertEqual(result["required_paths"]["b"], ["5", "3", "7"])

    def test_reroute_endpoint_and_forbidden_resource_validation(self):
        for path in ([5, 6], [5, 3, 7]):
            with self.subTest(path=path):
                planner = Decisions(action="action_reroute")
                planner.route = Mock(return_value=path)
                result = self.plan(planner=planner)
                self.assertEqual(result["status"], "BLOCKED")
                self.assertEqual(result["required_paths"]["b"], ["5", "3", "7"])

    def test_optional_order_preview_uses_dispatcher_contract(self):
        with patch("backend.app.services.llm_route_preview.preview_llm_route", return_value={"path": ["1", "4"]}) as preview:
            result = mapf.preview_order_route_for_mapf(self.graph, self.requests[0]["order"], "1", "4")
        self.assertEqual(result["path"], ["1", "4"])
        preview.assert_called_once_with(self.graph, "1", "4", selector="kev", model="kev-08b")


if __name__ == "__main__":
    unittest.main()
