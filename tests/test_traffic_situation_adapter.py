"""경로 DTO에 없는 실제 상태를 보완하되 이동/예약을 지우지 않는지 검증."""

from copy import deepcopy
import unittest

from backend.app.services.route_graph import load_route_graph
from backend.app.services.route_service import plan_route
from backend.app.services.traffic_joint_actions import TrafficSituation
from backend.app.services.traffic_situation_adapter import (
    TrafficInputDeferred, build_traffic_situation_input,
)
from backend.simulation.traffic_joint_actions import TestSelector


class TrafficInputAdapterTests(unittest.TestCase):
    def setUp(self):
        self.graph = load_route_graph()
        self.states = {
            rid: dict(
                status="IDLE", occupied_node=node, occupied_edge=None,
                navigation_id=None, route=None, goal_node=None,
                connected=True, fresh=True, active=False,
            ) for rid, node in [("robot1", "9"), ("robot2", "5"), ("robot3", "0")]
        }
        self.requests = [
            dict(order=dict(robot_id=rid, selector="kev", model="kev-08b"),
                 current_node=int(self.states[rid]["occupied_node"]), node=int(goal),
                 result=dict(path=plan_route(self.states[rid]["occupied_node"], goal, self.graph)["node_ids"]))
            for rid, goal in [("robot1", "11"), ("robot2", "8")]
        ]
        self.kwargs = dict(
            graph=self.graph, route_requests=self.requests, robot_states=self.states,
            reservations=(), protected_resources={}, wait_allowed_nodes=["9", "5", "0"],
            snapshot_id="adapter-test", request_metadata={
                "robot1": dict(request_order=1, waiting_seconds=12),
                "robot2": dict(request_order=2, waiting_seconds=5),
            },
        )

    def test_jhlee_shaped_requests_join_real_states_without_mutating_inputs(self):
        before = deepcopy(self.kwargs)
        payload = build_traffic_situation_input(**self.kwargs)
        self.assertEqual(self.kwargs, before)
        self.assertNotIn("status", self.requests[0])
        self.assertEqual(payload["config"]["zone_occupancy"], {
            "node:9": "robot1", "node:5": "robot2", "node:0": "robot3",
        })
        parked = payload["route_requests"][2]
        self.assertEqual(parked["result"]["path"], ["0"])
        self.assertEqual(parked["node"], "0")
        self.assertEqual(payload["route_requests"][0]["waiting_seconds"], 12)
        scene = TrafficSituation(payload)
        self.assertEqual(set(scene.plans), {"A1", "A2"})
        selector = TestSelector("A2")
        decision = scene.decide(selector)
        self.assertEqual(selector.calls, 1)
        self.assertEqual(scene.checked_plan(decision).steps[0].robot_id, "robot2")
        # 변환 결과를 수정해도 원본 입력에 영향을 주지 않는다.
        payload["graph"]["features"].clear()
        payload["route_requests"][0]["result"]["path"].clear()
        self.assertEqual(self.kwargs, before)

    def test_unsupported_actual_states_defer_before_selection(self):
        changes = [
            ("status", "NAVIGATING"), ("status", "WAITING"),
            ("occupied_edge", ["9", "7"]), ("navigation_id", "active-nav"),
            ("route", {"node_ids": ["9", "7", "11"]}),
            ("goal_node", "11"), ("active", True), ("order_id", "order1"),
            ("connected", False), ("fresh", False), ("occupied_node", None),
        ]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                args = deepcopy(self.kwargs)
                args["robot_states"]["robot1"][field] = value
                before = deepcopy(args)
                with self.assertRaises(TrafficInputDeferred):
                    build_traffic_situation_input(**args)
                self.assertEqual(args, before)

    def test_active_reservations_and_protection_are_never_erased(self):
        for resources in [
            dict(reservations=[{"resource": "node:7", "robot_id": "robot1"}]),
            dict(protected_resources={"node:7": "robot1"}),
            dict(reservations=None), dict(protected_resources=None),
        ]:
            with self.subTest(resources=resources):
                with self.assertRaises(TrafficInputDeferred):
                    build_traffic_situation_input(**{**self.kwargs, **resources})

    def test_stale_missing_or_contradictory_inputs_are_deferred(self):
        mutations = [
            lambda a: a["route_requests"][0].update(current_node=10),
            lambda a: a["route_requests"][0].update(status="NAVIGATING"),
            lambda a: a["robot_states"].pop("robot1"),
            lambda a: a["robot_states"]["robot2"].update(occupied_node="9"),
            lambda a: a["robot_states"]["robot1"].pop("navigation_id"),
            lambda a: a["request_metadata"].pop("robot1"),
            lambda a: a["route_requests"].append(deepcopy(a["route_requests"][0])),
            lambda a: a.update(mode="real"),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                args = deepcopy(self.kwargs)
                mutate(args)
                with self.assertRaises(TrafficInputDeferred):
                    build_traffic_situation_input(**args)

    def test_nonparticipant_occupancy_still_blocks_an_unsafe_route(self):
        self.states["robot3"]["occupied_node"] = "7"
        self.kwargs["wait_allowed_nodes"] = ["9", "5", "7"]
        scene = TrafficSituation(build_traffic_situation_input(**self.kwargs))
        self.assertEqual(set(scene.plans), {"HOLD_ALL"})
        selector = TestSelector()
        scene.decide(selector)
        self.assertEqual(selector.calls, 0)


if __name__ == "__main__":
    unittest.main()
