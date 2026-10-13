from fastapi import APIRouter, HTTPException

from ..schemas.order import CreateOrderRequest
from ..services.order_manager import order_manager
from ..services.mode_service import mode_manager
from ..services.workstation_manager import workstation_manager

router = APIRouter(prefix="/api/orders", tags=["orders"])


@router.post("")
async def create_order(payload: CreateOrderRequest):
    try:
        return order_manager.create_order(
            items=payload.items.model_dump(),
            total_quantity=payload.total_quantity,
            order_name=payload.order_name,
            driving_mode=payload.driving_mode,
            selector=payload.selector,
            model=payload.model,
        )

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("")
async def list_orders():
    return {"orders": order_manager.list_orders(),
            "workstations": workstation_manager.status(mode_manager.mode)}


@router.delete("/{order_id}")
async def cancel_order(order_id: str):
    if not order_manager.cancel_order(order_id):
        raise HTTPException(status_code=404, detail="취소할 주문이 없습니다.")
    return {"order_id": order_id, "status": "CANCELLED"}
