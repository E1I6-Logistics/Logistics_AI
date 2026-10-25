"""주문 이동에 선택된 Algorithm/AI 경로를 기존 주행기로 전달한다."""

import asyncio

from .mode_service import mode_manager, feature_engine
from .route_graph import load_route_graph


# 주문 경로를 배차한다.
async def dispatch_order_route(order, node, current_node, valid=None, *, charging_return=False):
    mode = order["mode"]
    if mode_manager.mode != mode:
        raise ValueError("운용 모드가 변경되었습니다.")

    required_path = None
    operation_mode = order.get("operation_mode", "ai" if order["driving_mode"] == "llm" else "algorithm")
    from .mapf.order_traffic_service import enabled, route_source

    use_traffic = enabled(order)
    configured_source = route_source() if use_traffic else "MODE"
    graph = load_route_graph()
    source = ("KEV" if feature_engine("route", operation_mode) == "ai" else "EXISTING_SHORTEST_PATH")
    if configured_source != "MODE":
        source = configured_source
    if use_traffic and source == "EXISTING_SHORTEST_PATH":
        from .mapf.traffic_route_provider import generate_traffic_route

        if current_node is None:
            raise ValueError("교통조정의 출발 노드를 확인할 수 없습니다.")
        required_path = generate_traffic_route(graph, current_node, node, source=source)["path"]
    elif feature_engine("route", operation_mode) == "ai":
        if current_node is None:
            raise ValueError("AI 경로의 출발 노드를 확인할 수 없습니다.")
        from .llm_route_preview import preview_llm_route

        try:
            result = await asyncio.to_thread(
                preview_llm_route, graph, current_node, node, selector=order["selector"], model=order["model"]
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

        snapshot = mock_fms.robot_snapshot(order["robot_id"], mode)
        if snapshot["goal_node"] is not None:
            raise ValueError("경로 생성 중 로봇의 현재 위치가 변경되었습니다.")
        if current_node is None:
            # Algorithm planning already supports an occupied edge as a
            # starting resource. AI paths require a confirmed start node.
            if (
                order["driving_mode"] != "standard"
                or snapshot["occupied_node"] is not None
                or snapshot["occupied_edge"] is None
            ):
                raise ValueError("로봇의 출발 노드를 확인할 수 없습니다.")
        elif snapshot["occupied_node"] != str(current_node):
            raise ValueError("경로 생성 중 로봇의 현재 위치가 변경되었습니다.")
        if use_traffic:
            from .mapf.order_traffic_service import coordinate_order_route

            return await coordinate_order_route(order, node, current_node, required_path,
                                                graph=graph, source=source, valid=valid)
        return mock_fms.navigate_to_node(order["robot_id"], node, required_path=required_path)

    from ..ros2.ros_gateway import ros_gateway
    from .fleet_manager import fleet_manager

    robot = fleet_manager.get_robot(order["robot_id"])
    if robot is None or not robot.connected or robot.occupied_node != str(current_node):
        raise ValueError("경로 생성 중 실제 로봇의 현재 위치가 변경되었습니다.")
    kwargs = {"_order_step": True, "required_path": required_path}
    if charging_return:
        kwargs["_charging_return"] = True
    return ros_gateway.navigate_to_node(order["robot_id"], node, **kwargs)
