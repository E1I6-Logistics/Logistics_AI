from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Literal
from ..services.scenario_service import load_config, scenario_manager

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])


class RunScenario(BaseModel):
    name: str
    route_source: Literal["EXISTING_SHORTEST_PATH", "KEV"] | None = None


# 시나리오를 목록으로 반환한다.
@router.get("")
async def list_scenarios():
    try:
        from backend.simulation.traffic_joint_action_scenario import route_source_choices, traffic_selector

        items = []
        for scenario in load_config()["scenarios"]:
            item = {"name": scenario["name"], "description": scenario["description"]}
            options = scenario.get("traffic_joint_action")
            item["test_goal"] = scenario.get("test_goal", "")
            item["group"] = scenario.get("test_group") or (
                "입력 연결" if options and route_source_choices(options)
                else "기본 동작 확인" if options
                else "일반 주문 공급" if "order_stream" in scenario
                else "기존 관제"
            )
            if options is not None:
                item.update(traffic_selector=traffic_selector(options), route_source_options=route_source_choices(options))
                if options.get("route_source") in item["route_source_options"]:
                    item["route_source"] = options["route_source"]
            items.append(item)
        return items
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


# 시나리오를 실행한다.
@router.post("/run", status_code=202)
async def run_scenario(payload: RunScenario):
    try:
        return scenario_manager.start(payload.name, route_source=payload.route_source)
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc


# 실행 중인 시나리오의 상태를 반환한다.
@router.get("/status")
async def scenario_status():
    return scenario_manager.status()


@router.post("/stop")
async def stop_order_supply():
    try:
        return scenario_manager.stop_order_supply()
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
