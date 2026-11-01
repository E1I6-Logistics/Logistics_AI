from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, StrictBool

from ..services import fms_restart

router = APIRouter(prefix="/api/system/web", tags=["system"])


class RestartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: StrictBool


@router.get("")
async def web_status():
    return fms_restart.status()


@router.post("/restart", status_code=202)
async def restart_web(request: RestartRequest):
    if not request.confirm:
        raise HTTPException(400, "전체 FMS 재시작 확인이 필요합니다")
    # 사용자가 요청한 대로 작업·주문 여부와 관계없이 기존 stop → start를 수행한다.
    return await run_in_threadpool(fms_restart.request_restart)
