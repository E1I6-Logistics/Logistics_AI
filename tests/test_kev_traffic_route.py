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
                kev_dispatcher.fms_mapf_coordinator.discard_pending("robot2")

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


if __name__ == "__main__":
    unittest.main()
