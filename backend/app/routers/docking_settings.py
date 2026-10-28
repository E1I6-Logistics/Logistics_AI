from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, StrictBool

from ..services.docking_settings import docking_settings

router = APIRouter(prefix="/api/docking/settings", tags=["docking"])


class UpdateDockingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contact_verification_enabled: StrictBool


@router.get("")
async def get_docking_settings():
    return docking_settings.snapshot()


@router.put("")
async def update_docking_settings(payload: UpdateDockingSettings):
    return docking_settings.set_contact_verification(payload.contact_verification_enabled)
