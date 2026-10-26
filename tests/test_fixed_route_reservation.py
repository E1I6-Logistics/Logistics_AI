"""LLM이 고른 노드 순서의 예약·대기·재개를 실제 모델 없이 확인한다."""

import unittest
from copy import deepcopy
from unittest.mock import patch
from types import SimpleNamespace

from backend.app.services.pathfinding import DistanceAStar
from backend.app.services.traffic_manager import TrafficManager
from backend.app.services.reservation import ReservationTable, edge_key
from backend.app.services.mock_data import MockFmsStore
from backend.app.services.real_navigation import RealNavigation
from backend.app.models.robot import Robot, RobotState, NavigationType


# 테스트에 사용할 그래프를 만든다.
def graph():
    points = {"0": (0, 0), "1": (1, 0), "2": (0, 1), "3": (2, 0)}
    edges = [("0", "1"), ("1", "3"), ("0", "2"), ("2", "3"), ("1", "0")]
    return {
        "type": "FeatureCollection",
        "features": [
            {"geometry": {"type": "Point", "coordinates": list(xy)}, "properties": {"id": n}}
            for n, xy in points.items()
        ]
        + [
            {
                "geometry": {"type": "MultiLineString"},
                "properties": {"id": f"e{i}", "startid": a, "endid": b, "weight": 1},
            }
            for i, (a, b) in enumerate(edges)
        ],
    }


# 테스트에 사용할 로봇 상태를 만든다.
def robot(rid="robot1", node="0"):
    xy = DistanceAStar(graph()).nodes[node]
    return dict(
        robot_id=rid,
        x=xy[0],
        y=xy[1],
        yaw=0,
        status="IDLE",
        current_node=node,
        occupied_node=node,
        occupied_edge=None,
        route=None,
        goal_node=None,
        navigation_id=None,
        request_order=None,
        stop_requested=False,
    )


class FixedReservationTests(unittest.TestCase):
    # 알고리즘 지름길이 있어도 LLM이 선택한 비최단 순서를 유지하는지 확인한다.
    # 최단 경로가 아닌 고정 경로도 유한한 대기 후 실행되는지 검증한다.
    def test_non_shortest_route_and_finite_wait(self):
        planner = DistanceAStar(graph())
        result = planner.plan_timed(
            "3",
            (0, 0),
            "0",
            blocked={edge_key("0", "2"): [(0, 10)]},
            now=1,
            speed_mps=1,
            safety_margin=0.2,
            required_path=["0", "2", "3"],
        )
        self.assertEqual(result["node_ids"], ["0", "2", "3"])
        self.assertGreaterEqual(result["segment_departures"][0], 10.2)
        self.assertEqual(planner.plan("0", "3").route, ("0", "1", "3"))

    # 기존 알고리즘 요청은 시간 예약에서도 자유롭게 경로를 탐색한다.
    # 알고리즘의 시간 기반 경로가 바뀌지 않는지 검증한다.
    def test_algorithm_timed_route_is_unchanged(self):
        result = DistanceAStar(graph()).plan_timed("3", (0, 0), "0", blocked={}, now=1, speed_mps=1, safety_margin=0.2)
        self.assertEqual(result["node_ids"], ["0", "1", "3"])

    # 정차 로봇으로 막힌 경로는 우회·양보로 바꾸지 않고 점유 해제 후 같은 순서로 재예약한다.
    # 차단 해제 후 같은 고정 경로를 재개하는지 검증한다.
    def test_blocked_wait_then_same_route(self):
        robots = {"robot1": robot(), "robot2": robot("robot2", "2")}
        traffic = TrafficManager(robots, speed_mps=1, safety_margin=0.2)
        traffic.request_navigation_after_stop("robot1", "3", now=1, graph=graph(), required_path=["0", "2", "3"])
        self.assertEqual(robots["robot1"]["status"], "WAITING")
        self.assertIsNone(robots["robot1"]["route"])
        self.assertFalse(traffic._concession)
        robots.pop("robot2")
        traffic.retry_waiting(2, graph())
        self.assertEqual(robots["robot1"]["route"]["node_ids"], ["0", "2", "3"])
        self.assertEqual(robots["robot1"]["route"]["route_source"], "llm")

    # 잘못된 유향 간선 입력으로 기존 상태나 예약을 바꾸지 않는지 확인한다.
    # 잘못된 경로가 상태 변경 없이 거부되는지 검증한다.
    def test_invalid_route_is_rejected_without_mutation(self):
        robots = {"robot1": robot()}
        traffic = TrafficManager(robots, speed_mps=1, safety_margin=0.2)
        before = deepcopy(robots)
        with self.assertRaises(ValueError):
            traffic.request_navigation_after_stop("robot1", "3", now=1, graph=graph(), required_path=["0", "3"])
        self.assertEqual(robots, before)
        self.assertEqual(traffic._reservations.snapshot(), ())

    # 예약 경계에서도 동일 방향 간선 재사용과 3회 이상 방문을 거부한다.
    # 고정 경로의 노드 방문 제한을 검증한다.
    def test_fixed_route_visit_limits(self):
        robots = {"robot1": robot()}
        traffic = TrafficManager(robots, speed_mps=1, safety_margin=0.2)
        with self.assertRaisesRegex(ValueError, "제한"):
            traffic.request_navigation_after_stop(
                "robot1", "3", now=1, graph=graph(), required_path=["0", "1", "0", "1", "3"]
            )
        self.assertEqual(robots["robot1"]["status"], "IDLE")

    # 노드 도착 후에는 남은 LLM 경로만 재예약하고 완료·취소 시 고정 계획을 해제한다.
    # 도착 후 경로를 재개하고 완료하는 흐름을 검증한다.
    def test_arrival_resume_and_finish(self):
        robots = {"robot1": robot()}
        traffic = TrafficManager(robots, speed_mps=1, safety_margin=0.2)
        traffic.request_navigation_after_stop("robot1", "3", now=1, graph=graph(), required_path=["0", "2", "3"])
        robots["robot1"].update(x=0, y=1)
        traffic.confirm_node_arrival("robot1", "2")
        traffic.wait_for_reservation_after_stop("robot1")
        traffic.retry_waiting(3, graph())
        self.assertEqual(robots["robot1"]["route"]["node_ids"], ["2", "3"])
        robots["robot1"].update(x=2, y=0)
        traffic.confirm_node_arrival("robot1", "3")
        self.assertEqual(robots["robot1"]["status"], "IDLE")
        self.assertNotIn("robot1", traffic._required_paths)
        traffic.request_navigation_after_stop("robot1", "3", now=5, graph=graph(), required_path=["3"])
        self.assertEqual(robots["robot1"]["status"], "IDLE")

    # 통로 중간 정지 후 재예약해도 선택한 진행 방향을 뒤집지 않는지 확인한다.
    # 간선 중간에서 재개해도 진행 방향을 유지하는지 검증한다.
    def test_mid_edge_resume_preserves_direction(self):
        result = DistanceAStar(graph()).plan_timed(
            "3",
            (0, 0.5),
            None,
            ("0", "2"),
            blocked={},
            now=2,
            speed_mps=1,
            safety_margin=0.2,
            required_path=["0", "2", "3"],
        )
        self.assertEqual(result["node_ids"], ["0", "2", "3"])

    # 재방문이 포함된 유효 경로를 노드별 위치가 아니라 경로 진행 순서로 예약하는지 확인한다.
    # 반복 방문하는 노드의 순서를 검증한다.
    def test_repeated_node_order(self):
        result = DistanceAStar(graph()).plan_timed(
            "3", (0, 0), "0", blocked={}, now=1, speed_mps=1, safety_margin=0.2, required_path=["0", "1", "0", "2", "3"]
        )
        self.assertEqual(result["node_ids"], ["0", "1", "0", "2", "3"])

    # 시뮬레이션 tick이 고정 경로를 실제로 이동하고 목적지에서 완료하는지 확인한다.
    # 시뮬레이션 로봇이 고정 경로를 따르는지 검증한다.
    def test_simulation_drives_fixed_route(self):
        nodes = DistanceAStar(graph()).nodes

        # 지정한 노드의 정보를 조회한다.
        def get_node(n):
            x, y = nodes[str(n)]
            return dict(id=str(n), x=x, y=y)

        with patch("backend.app.services.mock_data.get_node", side_effect=get_node), patch(
            "backend.app.services.mock_data.load_route_graph", return_value=graph()
        ), patch("backend.app.services.mock_data.monotonic", return_value=1):
            store = MockFmsStore(ReservationTable())
            store._robots = {"robot1": robot()}
            store._traffic._robots = store._robots
            result = store.navigate_to_node("robot1", "3", required_path=["0", "2", "3"])
            self.assertEqual(result["route"]["node_ids"], ["0", "2", "3"])
            for i in range(1, 300):
                store.advance_simulation(0.1, now=1 + i * 0.1)
                if store._robots["robot1"]["goal_node"] is None:
                    break
            self.assertEqual(store._robots["robot1"]["occupied_node"], "3")
            self.assertEqual(store._robots["robot1"]["status"], "IDLE")

    # 실제 실행기에는 고른 노드 순서 그대로 waypoint를 전송한다(ROS 전송만 가짜로 대체).
    # 실제 주행기가 고정 경유점을 전송하는지 검증한다.
    def test_real_executor_sends_fixed_waypoints(self):
        r = Robot("robot1")
        r.connected = True
        r.state = RobotState.IDLE
        r.x, r.y, r.yaw, r.pose_received_at = 0.0, 0.0, 0.0, 1.0
        fleet = SimpleNamespace(get_all_robots=lambda: [r], get_robot=lambda rid: r)
        sent = []
        nav = RealNavigation(
            fleet,
            lambda **kw: sent.append(kw),
            lambda *a, **k: None,
            lambda rid: None,
            {},
            0.1,
            reservations=ReservationTable(),
            clock=lambda: 1,
            mapf_ready=lambda rid: True,
        )
        with patch("backend.app.services.real_navigation.load_route_graph", return_value=graph()):
            nav.request("robot1", "3", NavigationType.GOAL, required_path=["0", "2", "3"])
            self.assertEqual(nav._robots["robot1"]["route"]["node_ids"], ["0", "2", "3"])
            nav._send("robot1", "3", 0, DistanceAStar(graph()))
        self.assertEqual([xy[:2] for xy in sent[0]["waypoints"]], [(0.0, 1.0)])
        self.assertEqual(nav._executing["robot1"][1], "2")
        self.assertFalse(nav._executing["robot1"][3])

    def test_turtlebot3_footprint_constants(self):
        """TurtleBot3 Burger 표준 풋프린트 반경(0.06m) 상수가 정의되어 있는지 검증."""
        from backend.app.services.traffic_manager import TURTLEBOT3_BURGER_RADIUS_M, TURTLEBOT3_CLEARANCE_DIAMETER_M
        self.assertEqual(TURTLEBOT3_BURGER_RADIUS_M, 0.06)
        self.assertEqual(TURTLEBOT3_CLEARANCE_DIAMETER_M, 0.12)
        tm = TrafficManager({"robot1": robot()}, speed_mps=1, safety_margin=0.2)
        self.assertEqual(tm.robot_radius_m, 0.06)

    def test_required_paths_mutual_vacating_resolves_deadlock(self):
        """[AI/MAPF] 두 로봇이 상대방의 현재 노드를 목적지로 하는 상호 대면/우회 경로일 때 교착 없이 완주하는지 검증."""
        from backend.app.services.route_graph import load_route_graph
        from backend.simulation.scenarios import prepare
        from backend.app.services.mock_data import MockFmsStore

        warehouse = load_route_graph()
        nodes = DistanceAStar(warehouse).nodes

        store = MockFmsStore(ReservationTable())
        store.reset()
        specs = [
            {"id": "robot1", "start": "9", "goal": "7"},
            {"id": "robot2", "start": "7", "goal": "9"},
            {"id": "robot3", "start": "0", "goal": None},
        ]
        with patch("backend.app.services.mock_data.load_route_graph", return_value=warehouse):
            # 시작 위치 배치
            store._robots["robot1"].update(x=nodes["9"][0], y=nodes["9"][1], current_node="9", occupied_node="9", occupied_edge=None, status="IDLE", goal_node=None, route=None)
            store._robots["robot2"].update(x=nodes["7"][0], y=nodes["7"][1], current_node="7", occupied_node="7", occupied_edge=None, status="IDLE", goal_node=None, route=None)
            store._robots["robot3"].update(x=nodes["0"][0], y=nodes["0"][1], current_node="0", occupied_node="0", occupied_edge=None, status="IDLE", goal_node=None, route=None)
            start_time = 100.0
            with patch("backend.app.services.mock_data.monotonic", return_value=start_time):
                store._last_tick = start_time - 0.1
                res1 = store.navigate_to_node("robot1", "7", required_path=["9", "7"])
                res2 = store.navigate_to_node("robot2", "9", required_path=["7", "8", "10", "9"])

            # 초기 예약 단계에서 두 로봇이 상호 비우기(cycle)를 감지하여 시간표를 확정해야 함
            self.assertIsNotNone(store._robots["robot1"]["route"])
            self.assertIsNotNone(store._robots["robot2"]["route"])

            # 시뮬레이션 진행
            for i in range(1, 300):
                store.advance_simulation(0.1, now=start_time + i * 0.1)
                r1_done = store._robots["robot1"]["status"] == "IDLE" and store._robots["robot1"]["occupied_node"] == "7"
                r2_done = store._robots["robot2"]["status"] == "IDLE" and store._robots["robot2"]["occupied_node"] == "9"
                if r1_done and r2_done:
                    break

            self.assertEqual(store._robots["robot1"]["status"], "IDLE")
            self.assertEqual(store._robots["robot1"]["occupied_node"], "7")
            self.assertEqual(store._robots["robot2"]["status"], "IDLE")
            self.assertEqual(store._robots["robot2"]["occupied_node"], "9")


if __name__ == "__main__":
    unittest.main()
