from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
import json
import re
from urllib.parse import quote
from pydantic import BaseModel
from ..services.order_stress_test import record_filename
from ..services.scenario_service import load_config, scenario_manager, RECORD_DIR

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])


class RunScenario(BaseModel):
    name: str


# 시나리오를 목록으로 반환한다.
@router.get("")
async def list_scenarios():
    try:
        return [{"name": s["name"], "description": s["description"]} for s in load_config()["scenarios"]]
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


# 시나리오를 실행한다.
@router.post("/run", status_code=202)
async def run_scenario(payload: RunScenario):
    try:
        return scenario_manager.start(payload.name)
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc


# 실행 중인 시나리오의 상태를 반환한다.
@router.get("/status")
async def scenario_status():
    return scenario_manager.status()


@router.post("/stop")
async def stop_scenario():
    return await scenario_manager.stop()


@router.get("/records")
async def scenario_records():
    records = []
    current = scenario_manager.status()
    for path in sorted(RECORD_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        summary = json.loads(path.read_text(encoding="utf-8"))
        if summary.get("state") == "running" and (summary.get("run_id") != current.get("run_id") or current["state"] not in ("running", "stopping")):
            summary["state"] = "interrupted"
        records.append(summary)
    return records


@router.get("/records/{run_id}")
async def scenario_record(run_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise HTTPException(404, detail="시험 기록이 없습니다.")
    for path in RECORD_DIR.glob("*.json"):
        summary = json.loads(path.read_text(encoding="utf-8"))
        if summary.get("run_id") == run_id:
            filename = path.name if path.name.startswith("자동배차테스트[") else record_filename(summary["started_at"])
            summary["file_name"] = filename
            return Response(json.dumps(summary, ensure_ascii=False, indent=2)+"\n", media_type="application/json",
                            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})
    raise HTTPException(404, detail="시험 기록이 없습니다.")


class ResetScenario(BaseModel):
    run_id: str


@router.post("/reset")
async def reset_scenario(payload: ResetScenario):
    try:
        return scenario_manager.reset(payload.run_id)
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
