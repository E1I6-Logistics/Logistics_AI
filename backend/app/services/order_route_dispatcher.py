"""[모듈: order_route_dispatcher.py - 주문 이동 Algorithm/AI 경로 주행 배차기]
- 역할:
  1. 일반 주문 경로(Algorithm 또는 단일 AI 경로)를 생성하여 시뮬레이션(`mock_fms`) 또는 실제 로봇(`ros_gateway`) 주행기로 전달한다.
  2. 시뮬레이션 AI 주행에서 `FMS_SIM_MAPF_ENABLED=1`인 경우, `kev_mapf/kev_dispatcher.py`의 KEV-MAPF 실행 게이트(`dispatch_mapf`)로 위임한다.
"""

from __future__ import annotations

import asyncio

from .kev_mapf.kev_dispatcher import (
    dispatch_mapf as _dispatch_mapf,
    mapf_enabled,
    runtime_for,
)
from .mode_service import feature_engine, mode_manager
from .route_graph import load_route_graph


async def dispatch_order_route(order, node, current_node, valid=None):
    """주문 이동에 선택된 Algorithm/AI 경로를 기존 주행기로 전달한다."""
    mode = order["mode"]
    if mode_manager.mode != mode:
        raise ValueError("운용 모드가 변경되었습니다.")

    operation_mode = order.get(
        "operation_mode", "ai" if order["driving_mode"] == "llm" else "algorithm"
    )

    # 시뮬레이션 AI 주행에서 KEV-MAPF 게이트가 활성화된 경우 kev_dispatcher로 위임
    if (
        mapf_enabled(mode)
        and feature_engine("route", operation_mode) == "ai"
    ):
        return await _dispatch_mapf(order, node, current_node, valid=valid)

    required_path = None
    result = None
    if feature_engine("route", operation_mode) == "ai":
        if current_node is None:
            raise ValueError("AI 경로의 출발 노드를 확인할 수 없습니다.")
        from .kev_mapf.kev_traffic import live_traffic
        from .llm_route_preview import preview_llm_route

        graph = load_route_graph()
        traffic = live_traffic(order["robot_id"], graph=graph) if mode == "simulation" else None
        try:
            result = await asyncio.to_thread(
                preview_llm_route,
                graph,
                current_node,
                node,
                selector=order["selector"],
                model=order["model"],
                traffic=traffic,
            )
        except (ValueError, RuntimeError) as exc:
            raise ValueError(f"AI 경로 생성 실패(N{current_node} → N{node}): {exc}") from exc
        required_path = result["path"]
        if mode_manager.mode != mode or load_route_graph() != graph:
            raise ValueError("AI 경로 생성 중 운용 모드 또는 지도가 변경되었습니다.")

    if valid is not None and not valid():
        raise ValueError("주문 단계가 변경되어 경로를 취소했습니다.")

    if mode == "simulation":
        from .mock_data import mock_fms
        _, coordinator = runtime_for(mode)

        snapshot = mock_fms.robot_snapshot(order["robot_id"], mode)
        if snapshot["goal_node"] is not None:
            raise ValueError("경로 생성 중 로봇의 현재 위치가 변경되었습니다.")
        if current_node is None:
            if (
                order["driving_mode"] != "standard"
                or snapshot["occupied_node"] is not None
                or snapshot["occupied_edge"] is None
            ):
                raise ValueError("로봇의 출발 노드를 확인할 수 없습니다.")
        elif snapshot["occupied_node"] != str(current_node):
            raise ValueError("경로 생성 중 로봇의 현재 위치가 변경되었습니다.")

        if required_path is not None:
            coordinator.stage_preview(
                order=order, current_node=current_node, goal_node=node, result=result
            )

        try:
            response = mock_fms.navigate_to_node(
                order["robot_id"], node, required_path=required_path
            )
        except Exception:
            if required_path is not None:
                coordinator.discard_pending(order["robot_id"])
            raise

        if required_path is not None:
            try:
                coordinator.register_preview(
                    order=order, current_node=current_node, goal_node=node, result=result
                )
            finally:
                coordinator.discard_pending(order["robot_id"])

        return response

    from ..ros2.ros_gateway import ros_gateway
    from .fleet_manager import fleet_manager

    robot = fleet_manager.get_robot(order["robot_id"])
    if robot is None or not robot.connected or robot.occupied_node != str(current_node):
        raise ValueError("경로 생성 중 실제 로봇의 현재 위치가 변경되었습니다.")
    return ros_gateway.navigate_to_node(
        order["robot_id"], node, _order_step=True, required_path=required_path
    )


__all__ = [
    "dispatch_order_route",
]
