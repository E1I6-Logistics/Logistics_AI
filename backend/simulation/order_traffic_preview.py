"""기존 관제 API·화면을 재사용하는 시뮬 전용 시험 서버.

ROS/MQTT 시작 함수를 실행하지 않는다. 기존 관제와 별도 프로세스·가상 저장소를
사용하며 실기체 모드 전환을 거부한다. 운영 서버의 대체 진입점이 아니다.
"""

import asyncio
from contextlib import asynccontextmanager, suppress
import os
from pathlib import Path
from time import monotonic

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.app.config import CORS_ORIGINS
from backend.app.routers import mode as mode_router
from backend.app.routers import commands, connections, map as map_routes, robots, websocket, orders, omx, scenarios
from backend.app.services.mode_service import mode_manager
from backend.app.services.order_manager import order_manager
from backend.app.services.mock_data import mock_fms
from backend.app.services.scenario_service import scenario_manager
from backend.app.services.simulation_order_runner import simulation_order_runner
from backend.app.services import order_traffic_service
from backend.app.services.websocket_manager import manager


async def run_preview_simulation():
    previous = monotonic()
    while True:
        await asyncio.sleep(.1)
        now = monotonic()
        dt, previous = now - previous, now
        await order_manager.tick()
        if mode_manager.mode == "simulation":
            mock_fms.advance_simulation(dt, now)
            for state in mock_fms.robot_snapshots("simulation"):
                await manager.broadcast({"type": "telemetry", "data": state})


@asynccontextmanager
async def simulation_lifespan(_app):
    operation_mode = os.getenv("FMS_PREVIEW_OPERATION_MODE", "ai")
    if operation_mode not in ("ai", "algorithm"):
        raise ValueError("시뮬 시험 모드는 ai 또는 algorithm이어야 합니다.")
    mode_manager.set_mode("simulation", operation_mode)
    log_dir = os.getenv("FMS_PREVIEW_LOG_DIR")
    if log_dir:
        order_traffic_service.LOG_DIR = Path(log_dir) / "order-traffic"
    task = asyncio.create_task(run_preview_simulation(), name="strategy-preview-simulation")
    try:
        yield
    finally:
        await scenario_manager.shutdown()
        await simulation_order_runner.shutdown()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="FMS simulation-only strategy preview", lifespan=simulation_lifespan)
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"])
for module in (map_routes, mode_router, robots, commands, connections, websocket, orders, omx, scenarios):
    app.include_router(module.router)
_original_snapshot = mode_router._snapshot


def preview_snapshot():
    return {**_original_snapshot(), "real_available": False, "simulation_preview": True,
            "simulation_preview_run_id": os.getenv("FMS_PREVIEW_RUN_ID"),
            "simulation_settings": {"speed_mps": mock_fms._traffic.speed_mps,
                                    "reservation_margin_seconds": mock_fms._traffic.safety_margin,
                                    "work_seconds": simulation_order_runner.work_seconds,
                                    "tick_seconds": .1}}


mode_router._snapshot = preview_snapshot


@app.middleware("http")
async def simulation_only(request: Request, call_next):
    if request.method == "PUT" and request.url.path == "/api/mode":
        payload = await request.json()
        if payload.get("mode") != "simulation":
            return JSONResponse(status_code=409, content={"detail": "시뮬 전용 시험 서버입니다. 실기체 모드를 사용할 수 없습니다."})
    return await call_next(request)
