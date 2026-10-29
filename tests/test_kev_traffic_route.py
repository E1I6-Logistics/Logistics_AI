"""단위 테스트: 다른 로봇을 고려하는 KEV 경로 생성(traffic-aware route) 검증."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

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

    def test_prepare_order_route_keeps_traffic_out_of_route_preview(self) -> None:
        """MAPF 상태는 경로 생성 모델 입력과 분리한다."""
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
                )
            finally:
                kev_dispatcher.runtime_for("simulation")[1].discard_pending("robot2")

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
        self.assertIn("generated path entered forbidden node", err or "")


if __name__ == "__main__":
    unittest.main()
