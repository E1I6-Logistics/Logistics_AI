from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, StrictBool

from ..services.dispatch_settings import dispatch_settings

router = APIRouter(prefix="/api/dispatch/settings", tags=["dispatch"])


class UpdateDispatchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    battery_voltage_check_enabled: StrictBool


@router.get("")
async def get_dispatch_settings():
    return dispatch_settings.snapshot()


@router.put("")
async def update_dispatch_settings(payload: UpdateDispatchSettings):
    return dispatch_settings.set_voltage_check(payload.battery_voltage_check_enabled)
