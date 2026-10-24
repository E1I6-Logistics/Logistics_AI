from __future__ import annotations

from fastapi import APIRouter

from ..services.mode_service import mode_manager
# 기존 import. 조회와 동기화는 connection_sync_service의 주기 작업으로 옮겼다.
# from ..services.zenoh_connection_manager import zenoh_connection_manager
# from ..ros2.ros_gateway import ros_gateway
from ..services.connection_sync_service import connection_sync_service

router = APIRouter(prefix="/api/connections", tags=["connections"])


# 연결을 조회한다.
@router.get("")
async def get_connections():
    if mode_manager.mode == "simulation":
        from ..services.mock_data import mock_fms

        return {"devices": mock_fms.connections()}

    # 기존 코드. 화면이 이 API를 부를 때만 연결을 갱신해, 화면을 닫으면 로봇 등록·OFFLINE 판정이 멈췄다.
    # try:
    #     devices = zenoh_connection_manager.connections()
    # except ConnectionError:
    #     return {"devices": zenoh_connection_manager.last_successful(), "status": "unavailable"}
    #
    # ros_gateway.sync_connected_robots(devices)
    #
    # return {"devices": devices}

    # Real 모드 연결 상태는 백엔드 주기 작업이 갱신한다.
    # 여기서는 마지막 결과만 돌려줘 화면의 주기 요청이 주행 잠금과 경합하지 않게 한다.
    return connection_sync_service.response()
