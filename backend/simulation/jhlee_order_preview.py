"""별도 프로세스에서만 원본 jhlee 주문 게이트를 사용하는 시뮬 시험 서버."""

import os

from fastapi import Request
from fastapi.responses import JSONResponse

from .order_traffic_jhlee import POLICY, install

# 비교에 명시한 공통 대기 한도와 우회 상한. 원본 기본값과 차이를 기록한다.
os.environ.setdefault("FMS_SIM_MAPF_MAX_WAIT", "60")
os.environ.setdefault("FMS_MAPF_MAX_REROUTES", "2")
os.environ.setdefault("FMS_MAPF_PRIORITY_MODE", "score")

from .order_traffic_preview import app, mode_router

provenance = install(preview_only=True)
_base_snapshot = mode_router._snapshot


def snapshot():
    result = _base_snapshot()
    result["order_traffic"] = {**result["order_traffic"], "selection_policy": POLICY,
        "available_policies": [POLICY], "scope": "JHLEE_NATIVE_ACTIVE_PENDING_GATE"}
    result["jhlee_experiment"] = provenance
    return result


mode_router._snapshot = snapshot


@app.middleware("http")
async def fixed_experiment(request: Request, call_next):
    if request.method == "PUT" and request.url.path.startswith("/api/mode"):
        return JSONResponse(status_code=409, content={"detail": "고정 조건의 jhlee 시뮬 시험 서버입니다. 실행 중 모드·판단기를 변경할 수 없습니다."})
    return await call_next(request)
