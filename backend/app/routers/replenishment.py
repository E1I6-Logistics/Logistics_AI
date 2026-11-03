"""수동 WAF 보충 API. 기존 주문·OMX MQTT 상태를 변경하지 않는다."""
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool
from ..services.mode_service import mode_manager
from ..services.pallet_replenishment import replenishment, ReplenishmentError

router = APIRouter(prefix="/api/replenishment", tags=["WAF replenishment"])


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: Literal["A", "B", "C", "D"]
    request_id: UUID
    confirm: StrictBool


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    confirm: StrictBool


@router.get("")
def status():
    return replenishment.snapshot()


def require_confirmation(confirm):
    if not confirm:
        raise HTTPException(400, "실제 WAF 동작 확인이 필요합니다")


@router.post("")
def start(request: StartRequest):
    require_confirmation(request.confirm)
    if mode_manager.mode != "real":
        raise HTTPException(409, "실제 로봇 모드에서만 재고 보충할 수 있습니다")
    try:
        return replenishment.start(request.item, str(request.request_id))
    except ReplenishmentError as exc:
        raise HTTPException(exc.status, exc.message) from exc


@router.post("/cancel")
def cancel(request: CancelRequest):
    require_confirmation(request.confirm)
    # 작업 중 UI 모드가 바뀌었어도 취소 요청은 허용한다.
    try:
        return replenishment.cancel(str(request.request_id))
    except ReplenishmentError as exc:
        raise HTTPException(exc.status, exc.message) from exc
