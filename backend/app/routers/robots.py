from __future__ import annotations

from fastapi import APIRouter

from ..services.mode_service import mode_manager
from ..services.fleet_manager import fleet_manager
from ..services.map_service import world_to_pixel

router = APIRouter(prefix="/api/robots", tags=["robots"])

# 주문 lock을 잠깐 못 잡았을 때 쓰는 값. 배차 상태를 모른다는 뜻이다.
_UNKNOWN_DISPATCH = {"assigned_order_id": None, "dispatch_available": None, "dispatch_block_reason": "status_busy"}


# 응답에 배차 가능 여부(assigned_order_id, dispatch_available, dispatch_block_reason)를 붙인다.
def _with_dispatch_status(rows, mode):
    from ..services.order_manager import order_manager

    status = order_manager.dispatch_status(mode)
    for row in rows:
        row.update(_UNKNOWN_DISPATCH if status is None else status.get(row["robot_id"], _UNKNOWN_DISPATCH))
    return rows


# 로봇을 가져온다.
@router.get("")
async def fetch_robots():
    # Simulation 모드
    if mode_manager.mode == "simulation":
        from ..services.mock_data import mock_fms

        # 기존 코드. 배차 가능 여부 필드를 붙이도록 바꾼다.
        # return mock_fms.robot_snapshots(mode_manager.mode)
        return _with_dispatch_status(mock_fms.robot_snapshots(mode_manager.mode), mode_manager.mode)

    # Real 모드
    result = []
    for robot in fleet_manager.get_all_robots():

        pixel_x = None
        pixel_y = None

        # AMCL 위치를 아직 받지 못했다면 좌표 변환하지 않음
        if robot.x is not None and robot.y is not None:
            pixel_x, pixel_y = world_to_pixel(robot.x, robot.y)

        result.append(
            {
                "robot_id": robot.robot_id,
                "status": robot.state.value,
                "pause_reason_code": robot.pause_reason_code,
                "pause_reason": robot.pause_reason,
                "battery": robot.battery,
                # 마지막 전압과, 배차 하한 판단에 쓰는 최근 최저 전압(V)
                "battery_voltage": robot.battery_voltage,
                "battery_voltage_min": robot.min_battery_voltage(),
                "x": robot.x,
                "y": robot.y,
                "yaw": robot.yaw,
                "current_node": robot.current_node,
                "route": robot.route,
                "pixel_x": pixel_x,
                "pixel_y": pixel_y,
                "connected": robot.connected,
                "map_pose_received": robot.x is not None and robot.y is not None,
                "mode": "real",
                "source": "ros2",
                "pose_source": "AMCL",
            }
        )

    # 기존 코드. 배차 가능 여부 필드를 붙이도록 바꾼다.
    # return result
    return _with_dispatch_status(result, "real")
