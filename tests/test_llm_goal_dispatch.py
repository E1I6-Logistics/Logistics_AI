"""Route dispatch must not move a robot on invalid or stale LLM output."""

import asyncio
import importlib.util
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from backend.app.schemas.command import GoalNodeRequest, StopRequest


# A test host without ROS 2 can still check the HTTP dispatch path.
if importlib.util.find_spec("action_msgs") is None:
    action = types.ModuleType("action_msgs")
    action.msg = types.ModuleType("action_msgs.msg")
    action.msg.GoalStatus = types.SimpleNamespace(STATUS_SUCCEEDED=4)
    sys.modules["action_msgs"] = action
    sys.modules["action_msgs.msg"] = action.msg

from backend.app.routers import commands


class GoalDispatchTests(unittest.IsolatedAsyncioTestCase):
    # 각 테스트에 필요한 초기 상태를 준비한다.
    def setUp(self):
        commands._llm_requests.clear()
        patches = [
            # Legacy path tests explicitly exercise the opt-out route.
            patch.dict(os.environ, {"FMS_SIM_MAPF_ENABLED": "0"}),
            patch.object(commands, "scenario_manager"),
            patch.object(commands, "mode_manager", types.SimpleNamespace(mode="simulation")),
            patch.object(commands, "_preview_start_node", return_value="0"),
            patch.object(commands, "load_route_graph", return_value={"map": 1}),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    # 일반 요청이 알고리즘 배차를 유지하는지 검증한다.
    async def test_standard_request_keeps_algorithm_dispatch(self):
        with patch.object(commands, "navigate_to_node", return_value={"success": True}) as dispatch:
            await commands.send_node_goal(GoalNodeRequest(robot_id="robot1", node_id=3))
        dispatch.assert_called_once_with("robot1", 3, "simulation")

    async def test_manual_ai_uses_mapf_by_default_without_direct_dispatch(self):
        from backend.app.services.kev_mapf import kev_dispatcher

        result = {"success": True, "mapf": {"enabled": True}, "llm_preview": {"path": ["0", "3"]}}
        async def dispatch(*args, valid, **kwargs):
            self.assertTrue(valid())
            return result

        with patch.dict(os.environ), patch.object(
            kev_dispatcher, "dispatch_mapf", new_callable=AsyncMock, side_effect=dispatch
        ) as gate, patch.object(commands, "navigate_to_node") as direct:
            os.environ.pop("FMS_SIM_MAPF_ENABLED", None)
            response = await commands.send_node_goal(
                GoalNodeRequest(robot_id="robot1", node_id=3, driving_mode="llm", selector="kev", model="kev-08b")
            )
        gate.assert_awaited_once()
        self.assertEqual(gate.call_args.args[0]["operation_mode"], "ai")
        self.assertTrue(response["mapf"]["enabled"])
        direct.assert_not_called()

    async def test_manual_mapf_invalidates_stopped_request(self):
        from backend.app.services.kev_mapf import kev_dispatcher

        async def stopped(*args, valid, **kwargs):
            commands._llm_requests.clear()
            valid()

        with patch.dict(os.environ, {"FMS_SIM_MAPF_ENABLED": "1"}), patch.object(
            kev_dispatcher, "dispatch_mapf", side_effect=stopped
        ), patch.object(commands, "navigate_to_node") as direct:
            with self.assertRaises(HTTPException) as error:
                await commands.send_node_goal(
                    GoalNodeRequest(robot_id="robot1", node_id=3, driving_mode="llm", selector="kev", model="kev-08b")
                )
        self.assertEqual(error.exception.status_code, 409)
        direct.assert_not_called()

    async def test_orders_use_the_same_mapf_gate_by_default(self):
        from backend.app.services import order_route_dispatcher as dispatcher
        from backend.app.services.mode_service import mode_manager

        for mode in ("simulation", "real"):
            order = {"robot_id": "robot1", "mode": mode, "operation_mode": "ai",
                     "driving_mode": "llm", "selector": "kev", "model": "kev-08b"}
            with patch.dict(os.environ), patch.object(mode_manager, "_mode", mode), patch.object(
                dispatcher, "_dispatch_mapf", new_callable=AsyncMock, return_value={"mapf": {"enabled": True}}
            ) as gate:
                os.environ.pop("FMS_SIM_MAPF_ENABLED", None)
                result = await dispatcher.dispatch_order_route(order, "3", "0")
            gate.assert_awaited_once_with(order, "3", "0", valid=None)
            self.assertTrue(result["mapf"]["enabled"])

    # 검증된 AI 경로가 예약 단계까지 전달되는지 검증한다.
    async def test_validated_path_reaches_reservation(self):
        with patch(
            "backend.app.services.llm_route_preview.preview_llm_route", return_value={"path": ["0", "2", "3"]}
        ), patch.object(commands, "navigate_to_node", return_value={"success": True}) as dispatch:
            result = await commands.send_node_goal(
                GoalNodeRequest(robot_id="robot1", node_id=3, driving_mode="llm", selector="laya", model="test")
            )
        dispatch.assert_called_once_with("robot1", 3, "simulation", required_path=["0", "2", "3"])
        self.assertEqual(result["llm_preview"]["path"], ["0", "2", "3"])
        self.assertFalse(commands._llm_requests)

    async def test_simulation_preview_supplies_traffic_and_registers_recovery_context(self):
        traffic = {"occupied_nodes": {7: "robot2"}, "robot_paths": {"robot2": [7, 9]}}
        with patch(
            "backend.app.services.kev_mapf.kev_traffic.live_traffic", return_value=traffic
        ) as collect, patch(
            "backend.app.services.llm_route_preview.preview_llm_route", return_value={"path": ["0", "2", "3"]}
        ) as preview, patch.object(commands, "navigate_to_node", return_value={"success": True}), patch(
            "backend.app.services.kev_mapf.kev_coordinator.FmsMapfCoordinator.register_preview"
        ) as register:
            await commands.send_node_goal(
                GoalNodeRequest(robot_id="robot1", node_id=3, driving_mode="llm", selector="kev", model="kev-08b")
            )
        collect.assert_called_once_with("robot1", graph={"map": 1})
        self.assertEqual(preview.call_args.kwargs["traffic"], traffic)
        self.assertEqual(register.call_args.kwargs["order"]["selector"], "kev")
        self.assertEqual(register.call_args.kwargs["result"]["path"], ["0", "2", "3"])

    # HTTP 요청으로 시뮬레이션 로봇이 목적지까지 이동하는지 검증한다.
    async def test_http_request_drives_simulation_to_target(self):
        from backend.app.services.mock_data import MockFmsStore
        from backend.app.services.reservation import ReservationTable
        from tests.test_fixed_route_reservation import graph, robot
        from backend.app.services.pathfinding import DistanceAStar

        test_graph = graph()
        nodes = DistanceAStar(test_graph).nodes

        # 지정한 노드의 정보를 조회한다.
        def get_node(node_id):
            x, y = nodes[str(node_id)]
            return {"id": str(node_id), "x": x, "y": y}

        with patch("backend.app.services.mock_data.get_node", side_effect=get_node), patch(
            "backend.app.services.mock_data.load_route_graph", return_value=test_graph
        ), patch("backend.app.services.mock_data.monotonic", return_value=1):
            store = MockFmsStore(ReservationTable())
            store._robots = {"robot1": robot()}
            store._traffic._robots = store._robots
            with patch("backend.app.services.mock_data.mock_fms", store), patch.object(
                commands, "load_route_graph", return_value=test_graph
            ), patch(
                "backend.app.services.llm_route_preview.preview_llm_route", return_value={"path": ["0", "2", "3"]}
            ):
                result = await commands.send_node_goal(
                    GoalNodeRequest(robot_id="robot1", node_id=3, driving_mode="llm", selector="laya", model="test")
                )
                self.assertEqual(result["route"]["node_ids"], ["0", "2", "3"])
                for step in range(1, 300):
                    store.advance_simulation(0.1, now=1 + step * 0.1)
                    if store._robots["robot1"]["goal_node"] is None:
                        break
                self.assertEqual(store._robots["robot1"]["occupied_node"], "3")

    async def test_real_mode_uses_mapf_gate_instead_of_direct_dispatch(self):
        from backend.app.services.kev_mapf import kev_dispatcher
        commands.mode_manager.mode = "real"
        with patch.object(kev_dispatcher, "dispatch_mapf", new_callable=AsyncMock,
                          return_value={"mapf": {"enabled": True}}) as gate, patch.object(commands, "navigate_to_node") as direct:
            await commands.send_node_goal(
                GoalNodeRequest(robot_id="robot1", node_id=3, driving_mode="llm", selector="kev", model="kev-08b")
            )
        gate.assert_awaited_once()
        self.assertEqual(gate.call_args.args[0]["mode"], "real")
        direct.assert_not_called()

    # 잘못된 경로를 배차하지 않는지 검증한다.
    async def test_invalid_route_does_not_dispatch(self):
        with patch(
            "backend.app.services.llm_route_preview.preview_llm_route", side_effect=ValueError("invalid_edge")
        ), patch.object(commands, "navigate_to_node") as dispatch:
            with self.assertRaises(HTTPException) as error:
                await commands.send_node_goal(
                    GoalNodeRequest(robot_id="robot1", node_id=3, driving_mode="llm", selector="laya", model="test")
                )
        self.assertEqual(error.exception.status_code, 400)
        dispatch.assert_not_called()

    # 추론 중 정지하면 뒤늦게 출발하지 않는지 검증한다.
    async def test_stop_during_inference_prevents_late_departure(self):
        ready, finish = asyncio.Event(), asyncio.Event()

        # 테스트에서 AI 경로 추론 결과를 제공한다.
        async def infer(*args, **kwargs):
            ready.set()
            await finish.wait()
            return {"path": ["0", "2", "3"]}

        with patch.object(commands, "run_in_threadpool", side_effect=infer), patch.object(
            commands, "navigate_to_node"
        ) as dispatch, patch("backend.app.services.mock_data.mock_fms.stop_robot", return_value={}):
            task = asyncio.create_task(
                commands.send_node_goal(
                    GoalNodeRequest(robot_id="robot1", node_id=3, driving_mode="llm", selector="laya", model="test")
                )
            )
            await ready.wait()
            await commands.stop_robot(StopRequest(robot_id="robot1"))
            finish.set()
            with self.assertRaises(HTTPException) as error:
                await task
        self.assertEqual(error.exception.status_code, 409)
        dispatch.assert_not_called()

    # 그래프가 변경되면 배차를 막는지 검증한다.
    async def test_changed_graph_prevents_dispatch(self):
        with patch.object(commands, "load_route_graph", side_effect=[{"map": 1}, {"map": 2}]), patch(
            "backend.app.services.llm_route_preview.preview_llm_route", return_value={"path": ["0", "3"]}
        ), patch.object(commands, "navigate_to_node") as dispatch:
            with self.assertRaises(HTTPException) as error:
                await commands.send_node_goal(
                    GoalNodeRequest(robot_id="robot1", node_id=3, driving_mode="llm", selector="laya", model="test")
                )
        self.assertEqual(error.exception.status_code, 409)
        dispatch.assert_not_called()

    # AI 경로 요청에 모델 지정이 필요한지 검증한다.
    def test_llm_request_requires_model(self):
        with self.assertRaises(ValidationError):
            GoalNodeRequest(robot_id="robot1", node_id=3, driving_mode="llm")


if __name__ == "__main__":
    unittest.main()
