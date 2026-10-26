from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from time import strftime

from ..models.robot import RobotState
from ..ros2.ros_gateway import ros_gateway
from ..services.fleet_manager import fleet_manager
from ..services.mode_service import mode_manager, feature_engines
from ..services.websocket_manager import manager
from .robots import fetch_robots
from ..schemas.robot import to_ui_robot_id

router = APIRouter(prefix="/api/mode", tags=["mode"])


class ModeRequest(BaseModel):
    mode: Literal["real", "simulation"]
    operation_mode: Literal["algorithm", "ai"] | None = None


# 현재 상태의 복사본을 만든다.
def _snapshot() -> dict[str, object]:
    from ..services.kev_mapf.kev_dispatcher import mapf_enabled, mapf_priority_mode

    return {
        "mode": mode_manager.mode,
        "operation_mode": mode_manager.operation_mode,
        "feature_engines": feature_engines(mode_manager.operation_mode),
        "real_available": ros_gateway._ros_node is not None,
        "simulation_active": mode_manager.mode == "simulation",
        "using_mock": mode_manager.mode == "simulation",
        "mapf_enabled": mapf_enabled(),
        "simulation_mapf_enabled": mapf_enabled("simulation"),
        "real_mapf_enabled": mapf_enabled("real"),
        "mapf_priority_mode": mapf_priority_mode(),
    }


# 모드를 조회한다.
@router.get("")
async def get_mode():
    return _snapshot()


# 모드를 설정한다.
@router.put("")
async def set_mode(payload: ModeRequest):
    from ..services.scenario_service import scenario_manager

    try:
        scenario_manager.ensure_available()
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    operation_mode = payload.operation_mode or mode_manager.operation_mode
    changing = payload.mode != mode_manager.mode or operation_mode != mode_manager.operation_mode
    if changing:
        from ..services.order_manager import order_manager

        if (
            any(row["status"] in ("PENDING", "IN_PROGRESS") for row in order_manager.list_orders())
            or order_manager.returning
            or order_manager.recovering
        ):
            raise HTTPException(409, detail="주문 또는 스테이션 복귀가 끝난 뒤 모드를 변경하세요.")
        if mode_manager.mode == "simulation":
            from ..services.mock_data import mock_fms

            if any(
                row["goal_node"] is not None or row["route"] is not None
                for row in mock_fms.robot_snapshots("simulation")
            ):
                raise HTTPException(409, detail="시뮬레이션 주행이 끝난 뒤 모드를 변경하세요.")
    if mode_manager.mode == "real" and changing:
        # 표시 모드만 바뀌어도 실제 Goal은 계속 실행되므로 작업 중에는 전환하지 않는다.
        with ros_gateway._navigation.lock:
            navigation = ros_gateway._navigation
            busy = [
                robot.robot_id
                for robot in fleet_manager.get_all_robots()
                if robot.state
                in (
                    RobotState.MOVING,
                    RobotState.WAITING,
                    RobotState.NAVIGATING,
                    RobotState.TASK_ASSIGNED,
                    RobotState.DOCKING,
                )
                or robot.order_id is not None
                or robot.route is not None
                or robot.navigation_type is not None
                or robot.robot_id in navigation._requests
                or robot.robot_id in navigation._executing
                or robot.robot_id in navigation._stopping
                or robot.robot_id in navigation._arrivals
                or robot.robot_id in ros_gateway._waiting_omx
                or (ros_gateway._ros_node is not None and ros_gateway._ros_node.has_active_auxiliary(robot.robot_id))
            ]
            if busy:
                print(
                    f"[{strftime('%Y-%m-%d %H:%M:%S')}] [WARN] [MODE] real->simulation rejected: "
                    f"active_robots={busy}",
                    flush=True,
                )
                raise HTTPException(status_code=409, detail=f"실제 로봇 작업 중에는 모드를 변경할 수 없습니다: {busy}")
            mode_manager.set_mode(payload.mode, operation_mode)
    else:
        mode_manager.set_mode(payload.mode, operation_mode)
    # 실제 모드 전환에서는 시뮬레이션 모듈을 로드하지 않는다.
    if payload.mode == "simulation":
        from ..services.mock_data import mock_fms

        states = mock_fms.robot_snapshots(payload.mode)
    else:
        states = None
    if states is None:
        states = await fetch_robots()
        for state in states:
            state["ui_id"] = to_ui_robot_id(state["robot_id"])
    await manager.broadcast(
        {
            "type": "system",
            "data": {
                "mode": mode_manager.mode,
                "operation_mode": mode_manager.operation_mode,
                "source": "mock" if mode_manager.mode == "simulation" else "ros2",
            },
        }
    )
    for state in states:
        await manager.broadcast({"type": "telemetry", "data": state})
    return _snapshot()
