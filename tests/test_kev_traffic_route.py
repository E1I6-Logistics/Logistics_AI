"""단위 테스트: 다른 로봇을 고려하는 KEV 경로 생성(traffic-aware route) 검증."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from backend.app.services.iterative_route_policy import (
    NEXT_NODE_INSTRUCTIONS,
    TRAFFIC_INSTRUCTIONS,
    next_node_options,
    next_node_state,
)
from backend.app.services.iterative_route_runner import run_selected_route
from backend.app.services.kev_mapf.kev_traffic import live_traffic, mapf_traffic
from backend.app.services.route_service import (
    build_compact_adjacency_graph,
    build_edge_weight_lookup,
    build_route_inputs,
)


def _sample_graph() -> dict:
    """1 -> 2 -> 4 (weight 2+2=4) 및 1 -> 3 -> 4 (weight 2+2=4) 대칭 경로를 가진 테스트 그래프."""
    points = {1: (0.0, 0.0), 2: (1.0, 1.0), 3: (1.0, -1.0), 4: (2.0, 0.0)}
    edges = [(1, 2, 2.0), (2, 4, 2.0), (1, 3, 2.0), (3, 4, 2.0)]
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [x, y]},
                "properties": {"id": str(n)},
            }
            for n, (x, y) in points.items()
        ]
        + [
            {
                "type": "Feature",
                "geometry": {"type": "MultiLineString"},
                "properties": {
                    "id": str(i + 10),
                    "startid": str(a),
                    "endid": str(b),
                    "weight": w,
                },
            }
            for i, (a, b, w) in enumerate(edges)
        ],
    }


def _prepared_graph(graph: dict) -> dict:
    compact = build_compact_adjacency_graph(graph, "test_graph.geojson", require_stored_weight=True)
    points, edges = build_route_inputs(graph, require_stored_weight=True)
    return {
        "compact_graph": compact,
        "nodes": sorted(int(n) for n in compact["nodes"]),
        "points": points,
        "edges": edges,
        "edge_weights": build_edge_weight_lookup(points, edges),
    }


class _StubSelector:
    """KEV 선택기를 모사하여 전달된 state, candidates, instructions를 기록하고 shared_with가 비어 있는 후보를 고른다."""

    name = "kev"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def select_choice(self, state: dict, candidates: dict, instructions: str, *, question_id: str = "q") -> dict:
        self.calls.append(
            {
                "state": state,
                "candidates": dict(candidates),
                "instructions": instructions,
                "question_id": question_id,
            }
        )
        for edge in state.get("available_edges", []):
            key = f"node_{edge['node']}"
            if key in candidates and not edge.get("shared_with"):
                return {"choice": key, "confidence": 0.95}
        return {"choice": next(iter(candidates)), "confidence": 0.5}


class KevTrafficRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = _sample_graph()
        self.prepared = _prepared_graph(self.graph)

    def test_no_traffic_keeps_policy_and_state_byte_identical(self) -> None:
        """traffic=None일 때 기존 후보, 가중치, state 키가 1바이트도 바뀌지 않는지 검증한다."""
        opts = next_node_options(
            self.prepared["compact_graph"],
            self.prepared["nodes"],
            1,
            [1],
            set(),
            {1: 1},
        )
        self.assertNotIn("traffic_fallback", opts)
        self.assertEqual(opts["candidate_nodes"], [2, 3])
        self.assertEqual(
            opts["available_edges"],
            [{"node": 2, "weight": 2}, {"node": 3, "weight": 2}],
        )

        state = next_node_state(
            self.prepared["compact_graph"],
            1,
            4,
            [1],
            set(),
            {1: 1},
            0.0,
            opts,
        )
        self.assertNotIn("other_robots", state)

        selector = _StubSelector()
        res = run_selected_route(
            selector,
            self.prepared,
            {"start": 1, "target": 4},
            candidate_scope="neighbors",
            deadline=5.0,
        )
        self.assertTrue(res["valid_path"])
        self.assertEqual(selector.calls[0]["instructions"], NEXT_NODE_INSTRUCTIONS)

    def test_occupied_node_is_hard_excluded_from_candidates(self) -> None:
        """다른 로봇이 현재 점유 중인 노드(2번)가 후보에서 하드 제외되어 3번 경로가 강제 선택되는지 검증한다."""
        traffic = {"occupied_nodes": {2: "robot1"}, "robot_paths": {"robot1": [2, 4]}}
        opts = next_node_options(
            self.prepared["compact_graph"],
            self.prepared["nodes"],
            1,
            [1],
            set(),
            {1: 1},
            target=4,
            traffic=traffic,
        )
        self.assertEqual(opts["candidate_nodes"], [3])
        self.assertFalse(opts["traffic_fallback"])
        self.assertEqual(
            opts["available_edges"],
            [{"node": 3, "weight": 2, "shared_with": []}],
        )

        selector = _StubSelector()
        res = run_selected_route(
            selector,
            self.prepared,
            {"start": 1, "target": 4},
            candidate_scope="neighbors",
            deadline=5.0,
            traffic=traffic,
        )
        self.assertTrue(res["valid_path"])
        self.assertEqual(res["path"], [1, 3, 4])
        self.assertEqual(res["api_call_count"], 0)

    def test_target_node_is_not_excluded_even_if_occupied(self) -> None:
        """목적지 노드(4번)는 다른 로봇이 점유 중이어도 후보에서 제외되지 않는지 검증한다."""
        traffic = {"occupied_nodes": {4: "robot1"}, "robot_paths": {}}
        opts = next_node_options(
            self.prepared["compact_graph"],
            self.prepared["nodes"],
            2,
            [1, 2],
            {(1, 2)},
            {1: 1, 2: 1},
            target=4,
            traffic=traffic,
        )
        self.assertEqual(opts["candidate_nodes"], [4])
        self.assertFalse(opts["traffic_fallback"])

    def test_fallback_when_all_candidates_are_occupied(self) -> None:
        """모든 후보(2, 3번)가 점유 중이면 빈 후보로 죽지 않고 원래 후보로 폴백하는지 검증한다."""
        traffic = {"occupied_nodes": {2: "robot1", 3: "robot3"}, "robot_paths": {}}
        opts = next_node_options(
            self.prepared["compact_graph"],
            self.prepared["nodes"],
            1,
            [1],
            set(),
            {1: 1},
            target=4,
            traffic=traffic,
        )
        self.assertEqual(opts["candidate_nodes"], [2, 3])
        self.assertTrue(opts["traffic_fallback"])

    def test_soft_shared_with_and_other_robots_in_state(self) -> None:
        """Active 로봇의 남은 경로가 shared_with 및 other_robots로 전달되고 가중치는 그대로인지 검증한다."""
        traffic = {"occupied_nodes": {99: "robot1"}, "robot_paths": {"robot1": [99, 2, 4]}}
        selector = _StubSelector()
        res = run_selected_route(
            selector,
            self.prepared,
            {"start": 1, "target": 4},
            candidate_scope="neighbors",
            deadline=5.0,
            traffic=traffic,
        )
        self.assertTrue(res["valid_path"])
        self.assertEqual(res["path"], [1, 3, 4])
        self.assertEqual( len(selector.calls), 1)
        call = selector.calls[0]
        self.assertIn(TRAFFIC_INSTRUCTIONS, call["instructions"])
        self.assertEqual(
            call["state"]["available_edges"],
            [
                {"node": 2, "weight": 2, "shared_with": ["robot1"]},
                {"node": 3, "weight": 2, "shared_with": []},
            ],
        )
        self.assertEqual(
            call["state"]["other_robots"],
            [{"robot_id": "robot1", "occupied_nodes": [99], "remaining_path": [99, 2, 4]}],
        )

    def test_live_and_mapf_traffic_builders(self) -> None:
        """live_traffic(간선 양 끝 점유 및 Active 경로만 포함)과 mapf_traffic 변환을 검증한다."""
        snapshots = [
            {
                "robot_id": "robot1",
                "status": "NAVIGATING",
                "occupied_node": None,
                "occupied_edge": "10",
                "route": {"node_ids": ["1", "2", "4"], "segment_index": 0},
            },
            {
                "robot_id": "robot2",
                "status": "IDLE",
                "occupied_node": "3",
                "occupied_edge": None,
                "route": None,
            },
            {
                "robot_id": "robot3",
                "status": "IDLE",
                "occupied_node": "1",
                "occupied_edge": None,
                "route": {"node_ids": ["1", "3", "4"], "segment_index": 0},
            },
        ]
        live = live_traffic("robot3", graph=self.graph, snapshots=snapshots)
        self.assertEqual(live["occupied_nodes"], {1: "robot1", 2: "robot1", 3: "robot2"})
        self.assertEqual(live["robot_paths"], {"robot1": [1, 2, 4]})

        robots = {
            "a": {"id": "a", "current_node": 1, "path": [1, 2, 4]},
            "b": {"id": "b", "current_node": 3, "path": [3, 4]},
        }
        m_traffic = mapf_traffic(robots, "b", occupied={"node:2": "a"})
        self.assertEqual(m_traffic["occupied_nodes"], {1: "a", 2: "a"})
        self.assertEqual(m_traffic["robot_paths"], {"a": [1, 2, 4]})

    def test_prepare_order_route_passes_live_traffic_to_preview(self) -> None:
        """prepare_order_route가 FMS 실시간 traffic을 preview_llm_route에 전달하는지 검증한다."""
        from backend.app.services.kev_mapf import kev_dispatcher

        order = {
            "robot_id": "robot2",
            "mode": "simulation",
            "driving_mode": "llm",
            "operation_mode": "ai",
            "selector": "kev",
            "model": "kev-08b",
        }
        fake_snap = {
            "robot_id": "robot2",
            "status": "IDLE",
            "connection_state": "ONLINE",
            "stop_requested": False,
            "occupied_node": "1",
            "occupied_edge": None,
            "goal_node": None,
            "navigation_id": None,
            "route": None,
        }
        other_snap = {
            "robot_id": "robot1",
            "status": "NAVIGATING",
            "occupied_node": "2",
            "occupied_edge": None,
            "route": {"node_ids": ["1", "2", "4"], "segment_index": 1},
        }
        fake_mock_data = unittest.mock.MagicMock()
        fake_mock_data.mock_fms.robot_snapshot.return_value = fake_snap
        fake_mock_data.mock_fms.robot_snapshots.return_value = [other_snap, fake_snap]

        with patch.dict(
            "sys.modules", {"backend.app.services.mock_data": fake_mock_data}
        ), patch.object(
            kev_dispatcher, "mode_manager", unittest.mock.MagicMock(mode="simulation")
        ), patch.object(
            kev_dispatcher, "load_route_graph", return_value=self.graph
        ), patch(
            "backend.app.services.llm_route_preview.preview_llm_route",
            return_value={"path": ["1", "3", "4"]},
        ) as preview_mock:
            try:
                prepared = asyncio.run(kev_dispatcher.prepare_order_route(order, "4", "1"))
                self.assertEqual(prepared["required_path"], ["1", "3", "4"])
                preview_mock.assert_called_once_with(
                    self.graph,
                    "1",
                    "4",
                    selector="kev",
                    model="kev-08b",
                    traffic={"occupied_nodes": {2: "robot1"}, "robot_paths": {"robot1": [2, 4]}},
                )
            finally:
                kev_dispatcher.runtime_for("simulation")[1].discard_pending("robot2")

    def test_direction_cues_guide_shortest_path_selection(self) -> None:
        """방향 정보(dist_to_target, progress, direction)가 1-hop 가중치 함정을 피해 최단 경로로 유도하는지 검증한다."""
        from backend.app.services.iterative_route_policy import DIRECTION_INSTRUCTIONS

        # 1(0,0)에서 목적지 4(2,0)으로 갈 때:
        # - 후보 5(-0.3, 0): 1-hop weight는 0.3으로 더 작지만 서쪽(W)으로 후퇴(progress < 0) 후 4까지 멀리 우회(weight 3.0)
        # - 후보 2(1, 0): 1-hop weight는 1.0이지만 동쪽(E) 목적지 방향으로 전진(progress > 0) 후 4로 직행(weight 1.0)
        points = {1: (0.0, 0.0), 2: (1.0, 0.0), 4: (2.0, 0.0), 5: (-0.3, 0.0)}
        edges = [(1, 5, 0.3), (5, 4, 3.0), (1, 2, 1.0), (2, 4, 1.0)]
        graph = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [x, y]},
                    "properties": {"id": str(n)},
                }
                for n, (x, y) in points.items()
            ]
            + [
                {
                    "type": "Feature",
                    "geometry": {"type": "MultiLineString"},
                    "properties": {"id": str(i + 10), "startid": str(a), "endid": str(b), "weight": w},
                }
                for i, (a, b, w) in enumerate(edges)
            ],
        }
        prepared = _prepared_graph(graph)

        opts = next_node_options(
            prepared["compact_graph"],
            prepared["nodes"],
            1,
            [1],
            set(),
            {1: 1},
            target=4,
            points=prepared["points"],
        )
        self.assertEqual(
            opts["available_edges"],
            [
                {"node": 2, "weight": 1, "dist_to_target": 1.0, "progress": 1.0, "direction": "E"},
                {"node": 5, "weight": 0.3, "dist_to_target": 2.3, "progress": -0.3, "direction": "W"},
            ],
        )

        state = next_node_state(
            prepared["compact_graph"],
            1,
            4,
            [1],
            set(),
            {1: 1},
            0.0,
            opts,
            points=prepared["points"],
        )
        self.assertEqual(state["target_distance"], 2.0)
        self.assertEqual(state["target_direction"], "E")

        class _DirectionAwareSelector:
            name = "kev"

            def __init__(self) -> None:
                self.calls: list[dict] = []

            def select_choice(self, st: dict, candidates: dict, instructions: str, *, question_id: str = "q") -> dict:
                self.calls.append({"state": st, "instructions": instructions})
                # progress > 0 (목적지 접근)이면서 남은 직선거리 + 간선 가중치가 가장 작은 후보 선택
                best = min(
                    st["available_edges"],
                    key=lambda e: (e.get("progress", 0.0) <= 0, e["weight"] + e.get("dist_to_target", 0.0)),
                )
                return {"choice": f"node_{best['node']}", "confidence": 0.95}

        selector = _DirectionAwareSelector()
        res = run_selected_route(
            selector,
            prepared,
            {"start": 1, "target": 4, "expected_path": [1, 2, 4], "expected_distance": 2.0},
            candidate_scope="neighbors",
            deadline=5.0,
            include_direction=True,
        )
        self.assertTrue(res["valid_path"])
        self.assertEqual(res["path"], [1, 2, 4])
        self.assertTrue(res["exact_path_match"])
        self.assertIn(DIRECTION_INSTRUCTIONS, selector.calls[0]["instructions"])

    def test_major_task_nodes_excluded_as_intermediate_transit(self) -> None:
        """실제 창고 맵(test.geojson)에서 주요 노드(0..6)가 출발·목적지 사이의 경유지에서 제외되는지 검증한다."""
        from backend.app.services.pathfinding import DistanceAStar
        from backend.app.services.route_service import load_route_graph, plan_route

        warehouse = load_route_graph()
        prepared = _prepared_graph(warehouse)

        # N5 -> N3: N6(Pickup 2)이 제외되어 N5에서 후보가 [7] 하나만 남음
        opts_5_to_3 = next_node_options(
            prepared["compact_graph"],
            prepared["nodes"],
            5,
            [5],
            set(),
            {5: 1},
            target=3,
            points=prepared["points"],
        )
        self.assertEqual(opts_5_to_3["candidate_nodes"], [7])

        # N6 -> N4: N5(Pickup 1)가 제외되어 N6에서 후보가 [11] 하나만 남음
        opts_6_to_4 = next_node_options(
            prepared["compact_graph"],
            prepared["nodes"],
            6,
            [6],
            set(),
            {6: 1},
            target=4,
            points=prepared["points"],
        )
        self.assertEqual(opts_6_to_4["candidate_nodes"], [11])

        # N5 -> N6: 출발·목적지가 모두 주요 노드이면 6은 목적지이므로 제외되지 않고 직접 선택됨
        opts_5_to_6 = next_node_options(
            prepared["compact_graph"],
            prepared["nodes"],
            5,
            [5],
            set(),
            {5: 1},
            target=6,
            points=prepared["points"],
        )
        self.assertEqual(opts_5_to_6["candidate_nodes"], [6])

        self.assertEqual(plan_route("5", "3", warehouse)["node_ids"], ["5", "7", "11", "3"])
        self.assertEqual(plan_route("6", "4", warehouse)["node_ids"], ["6", "11", "12", "4"])
        self.assertEqual(plan_route("0", "6", warehouse)["node_ids"], ["0", "10", "8", "7", "11", "6"])
        self.assertEqual(plan_route("5", "6", warehouse)["node_ids"], ["5", "6"])

        planner = DistanceAStar(warehouse)
        self.assertEqual(planner.plan("5", "3").route, ("5", "7", "11", "3"))
        self.assertEqual(planner.plan("6", "4").route, ("6", "11", "12", "4"))
        self.assertEqual(planner.plan("0", "6").route, ("0", "10", "8", "7", "11", "6"))
        self.assertEqual(planner.plan("5", "6").route, ("5", "6"))

    def test_kev_mapf_reroute_failure_holds_without_changing_kev_action(self) -> None:
        """KEV가 고른 REROUTE 실패 시 코드가 WAIT를 대신 고르지 않고 HOLD하는지 검증한다."""
        from backend.app.services.kev_mapf.kev_engine import KevMAPF
        from backend.app.services.kev_mapf.kev_graph import Graph
        from backend.app.services.route_service import load_route_graph

        warehouse = Graph(load_route_graph())
        robots = {
            "robot3": {
                "id": "robot3",
                "current_node": 5,
                "goal_node": 3,
                "path": [5, 7, 11, 3],
                "selector": "kev",
                "model": "kev-08b",
                "source": "initial",
            },
            "robot2": {
                "id": "robot2",
                "current_node": 6,
                "goal_node": 4,
                "path": [6, 11, 12, 4],
                "selector": "kev",
                "model": "kev-08b",
                "source": "initial",
            },
        }

        class _RerouteThenWaitLive:
            calls = 0
            forced = 0
            model_seconds = 0.0
            warnings: list[str] = []

            def score_many(self, state: dict, robot_ids: list[str]) -> dict[str, float]:
                return {"robot3": 0.9, "robot2": 0.2}

            def pick(self, state: dict, choices: dict, instructions: str, question_id: str) -> str:
                if "action_reroute" in choices:
                    return "action_reroute"
                return next(iter(choices))

            def route(self, *args, **kwargs):
                raise RuntimeError("no detour available")

        engine = KevMAPF(
            warehouse,
            robots,
            _RerouteThenWaitLive(),
            {"priority_mode": "score", "wait_allowed_nodes": [5, 6]},
        )
        advisory = engine.resolve()
        self.assertEqual(advisory["status"], "BLOCKED")
        self.assertEqual(len(advisory["decisions"]), 1)
        self.assertEqual(advisory["decisions"][0]["action"], "HOLD")
        self.assertEqual(advisory["decisions"][0]["yield_robot"], "robot2")
        self.assertIsNone(advisory["decisions"][0]["wait_node"])
        self.assertEqual(advisory["decisions"][0]["reason"], "REROUTE_FAILED")
        self.assertIn("no detour available", advisory["decisions"][0]["details"])

    def test_bottleneck_occupied_node_and_start_major_reentry_prevent_dead_end(self) -> None:
        """병목 노드(N11)가 점유 중일 때 목적지(N6) 도달 불능 우회 및 출발 주요 노드(N4) 재진입 막다른 길을 방지하는지 검증한다."""
        from backend.app.services.route_service import load_route_graph

        warehouse = load_route_graph()
        prepared = _prepared_graph(warehouse)
        traffic = {"occupied_nodes": {11: "robot3"}, "robot_paths": {"robot3": [11, 3]}}

        # N4 -> N12 이동 직후: 출발 주요 노드 4로의 역방향 재진입은 금지되고,
        # N11을 제외하면 N6에 도달할 수 없으므로 traffic_fallback=True로 11이 유지된다.
        opts_at_12 = next_node_options(
            prepared["compact_graph"],
            prepared["nodes"],
            12,
            [4, 12],
            {(4, 12)},
            {4: 1, 12: 1},
            target=6,
            traffic=traffic,
            points=prepared["points"],
        )
        self.assertNotIn(4, opts_at_12["candidate_nodes"])
        self.assertIn(11, opts_at_12["candidate_nodes"])
        self.assertTrue(opts_at_12["traffic_fallback"])

        class _ProgressSelector:
            name = "kev"

            def select_choice(self, st: dict, candidates: dict, instructions: str, *, question_id: str = "q") -> dict:
                best = min(
                    st["available_edges"],
                    key=lambda e: (e.get("progress", 0.0) <= 0, e["weight"] + e.get("dist_to_target", 0.0)),
                )
                return {"choice": f"node_{best['node']}", "confidence": 0.95}

        res = run_selected_route(
            _ProgressSelector(),
            prepared,
            {"start": 4, "target": 6},
            candidate_scope="neighbors",
            deadline=5.0,
            traffic=traffic,
            include_direction=True,
        )
        self.assertTrue(res["valid_path"])
        self.assertEqual(res["path"], [4, 12, 11, 6])

    def test_check_segment_permission_ignores_transient_target_occupancy_before_reservation_window(self) -> None:
        """목적 노드의 예약 시작 시각(destination.start) 이전에 다른 주행 중 로봇이 일시 통과 중이면 replan하지 않는지 검증한다."""
        from backend.app.services.pathfinding import DistanceAStar
        from backend.app.services.route_service import load_route_graph
        from backend.app.services.traffic_manager import TrafficManager

        warehouse = load_route_graph()
        planner = DistanceAStar(warehouse)

        def _make_robot(rid: str, node: str) -> dict:
            xy = planner.nodes[node]
            return {
                "robot_id": rid,
                "x": xy[0],
                "y": xy[1],
                "yaw": 0.0,
                "status": "IDLE",
                "current_node": node,
                "occupied_node": node,
                "occupied_edge": None,
                "goal_node": None,
                "navigation_id": None,
                "route": None,
                "request_order": None,
                "stop_requested": False,
            }

        robots = {"robot1": _make_robot("robot1", "7"), "robot2": _make_robot("robot2", "11")}
        tm = TrafficManager(robots, speed_mps=1.0, safety_margin=0.2)
        tm.request_navigation_after_stop("robot2", "3", now=0.0, graph=warehouse)
        tm.request_navigation_after_stop("robot1", "6", now=0.0, graph=warehouse)

        r1 = robots["robot1"]
        r2 = robots["robot2"]
        self.assertIsNotNone(r1["route"])
        self.assertIsNotNone(r2["route"])

        # robot2가 아직 node:11을 점유 중이지만 활성 스케줄(departure_at != None)로 주행 중이고,
        # robot1은 7->11 간선 출발 시각 직후(robot1의 node:11 destination.start 이전)일 때 allowed여야 한다.
        import math

        dep_r1 = r1["route"]["segment_departures"][0]
        arr_r1 = dep_r1 + math.dist(planner.nodes["7"], planner.nodes["11"]) / tm.speed_mps
        perm = tm.check_segment_permission(
            "robot1", now=dep_r1 + 0.05, expected_arrival_at=arr_r1, graph=warehouse
        )
        self.assertEqual(perm.decision, "allowed")

        # 반면 robot2의 스케줄이 해제(정지/대기)된 상태로 node:11을 막고 있으면 즉시 replan이어야 한다.
        r2["route"]["departure_at"] = None
        perm_blocked = tm.check_segment_permission(
            "robot1", now=dep_r1 + 0.05, expected_arrival_at=arr_r1, graph=warehouse
        )
        self.assertEqual(perm_blocked.decision, "replan")
        self.assertEqual(perm_blocked.reason, "occupied")

    def test_dominated_detour_neighbor_is_pruned_and_parallel_corridors_preserved(self) -> None:
        """필수 경유지(N12)를 거칠 수밖에 없는 우회 이웃(N7)은 제거되고, 실제 평행 대안 경로(N8->N7/N12)는 유지되는지 검증한다."""
        from backend.app.services.route_service import load_route_graph

        warehouse = load_route_graph()
        prepared = _prepared_graph(warehouse)
        traffic = {"occupied_nodes": {12: "robot1"}, "robot_paths": {"robot1": [12, 4]}}

        # N6 -> N11 이동 직후(target=4): N7로 가더라도 반드시 N12를 거쳐야 하므로 N7은 지배되어 제거되고 [12]만 남는다.
        opts_at_11 = next_node_options(
            prepared["compact_graph"],
            prepared["nodes"],
            11,
            [6, 11],
            {(6, 11)},
            {6: 1, 11: 1},
            target=4,
            traffic=traffic,
            points=prepared["points"],
        )
        self.assertEqual(opts_at_11["candidate_nodes"], [12])

        res = run_selected_route(
            _StubSelector(),
            prepared,
            {"start": 6, "target": 4},
            candidate_scope="neighbors",
            deadline=5.0,
            traffic=traffic,
            include_direction=True,
        )
        self.assertTrue(res["valid_path"])
        self.assertEqual(res["path"], [6, 11, 12, 4])
        self.assertEqual(res["api_call_count"], 0)

        # 반면 N10 -> N8 이동 직후(target=3): N7(8->7->11->3)과 N12(8->12->11->3)는 서로를 거치지 않는 평행 대안이므로 둘 다 유지된다.
        opts_at_8 = next_node_options(
            prepared["compact_graph"],
            prepared["nodes"],
            8,
            [0, 10, 8],
            {(0, 10), (10, 8)},
            {0: 1, 10: 1, 8: 1},
            target=3,
            points=prepared["points"],
        )
        self.assertEqual(sorted(opts_at_8["candidate_nodes"]), [7, 12])

    def test_kev_mapf_edge_reroute_rejects_both_endpoints_loop(self) -> None:
        """KEV-MAPF 간선 충돌(edge:11:12) 우회 시 양 끝점(11, 12)을 모두 그대로 지나는 ㄷ자 루프(6->11->7->8->12->4)를 거부하고 WAIT로 폴백하는지 검증한다."""
        from backend.app.services.kev_mapf.kev_engine import KevMAPF
        from backend.app.services.kev_mapf.kev_graph import Graph
        from backend.app.services.route_service import load_route_graph

        warehouse = Graph(load_route_graph())
        robots = {
            "robot1": {
                "id": "robot1",
                "current_node": 12,
                "goal_node": 3,
                "path": [12, 11, 3],
                "selector": "kev",
                "model": "kev-08b",
                "source": "initial",
            },
            "robot3": {
                "id": "robot3",
                "current_node": 6,
                "goal_node": 4,
                "path": [6, 11, 12, 4],
                "selector": "kev",
                "model": "kev-08b",
                "source": "initial",
            },
        }

        class _LoopRerouteLive:
            calls = 0
            forced = 0
            model_seconds = 0.0
            warnings: list[str] = []

            def score_many(self, state: dict, robot_ids: list[str]) -> dict[str, float]:
                return {"robot1": 0.9, "robot3": 0.2}

            def pick(self, state: dict, choices: dict, instructions: str, question_id: str) -> str:
                if "action_reroute" in choices:
                    return "action_reroute"
                return next(iter(choices))

            def route(self, *args, **kwargs):
                return [6, 11, 7, 8, 12, 4]

        engine = KevMAPF(
            warehouse,
            robots,
            _LoopRerouteLive(),
            {"priority_mode": "score", "wait_allowed_nodes": [6, 12]},
        )
        ok_path, err = engine._reroute(robots["robot3"], "edge:11:12", {"kind": "edge", "edge": [11, 12], "robots": ["robot1", "robot3"]})
        self.assertIsNone(ok_path)
        self.assertIn("both endpoints of forbidden edge", err or "")


if __name__ == "__main__":
    unittest.main()
