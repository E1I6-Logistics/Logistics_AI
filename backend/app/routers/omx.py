import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool

from ..models.omx import OMX
from ..services.mqtt_manager import mqtt_manager
from ..services.map_service import world_to_pixel
from ..services.mode_service import mode_manager
from ..services.omx_maintenance import restart_service, target_for

OMX_POSITIONS = {
    "omx1": (0.8085766434669495, -0.521459),
    "omx2": (1.5943385362625122, -0.521459),
    "omx3": (1.989379, 0.265518),
    "omx4": (1.989379, 0.629562),
}

router = APIRouter(prefix="/api/omx", tags=["omx"])


@router.get("")
async def get_omx_devices():
    result = []
    # 미수신 장비도 지도에서 선택해 연결 상태를 확인할 수 있다.
    for omx_id, (world_x, world_y) in OMX_POSITIONS.items():
        omx = mqtt_manager.get_omx(omx_id)
        if omx is None:
            omx = OMX(omx_id, None)
            omx.connected = False
            omx.last_seen = 0
        pixel_x, pixel_y = world_to_pixel(world_x, world_y)
        result.append({**omx.snapshot(), "x": world_x, "y": world_y,
                       "pixel_x": pixel_x, "pixel_y": pixel_y,
                       "restart_available": target_for(omx_id) is not None})
    return result


class RestartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: StrictBool


@router.post("/{omx_id}/restart")
async def restart_omx(omx_id: str, request: RestartRequest):
    if omx_id not in OMX_POSITIONS:
        raise HTTPException(404, "등록되지 않은 OMX입니다")
    if not request.confirm:
        raise HTTPException(400, "재시작 확인이 필요합니다")
    if mode_manager.mode != "real":
        raise HTTPException(409, "실제 로봇 모드에서만 OMX 서비스를 재시작할 수 있습니다")
    # HTTP 이벤트 루프를 SSH 응답 대기로 멈추지 않는다.
    return await asyncio.to_thread(restart_service, mqtt_manager, omx_id)
