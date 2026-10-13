from fastapi import APIRouter, HTTPException

from ..schemas.order import CreateOrderRequest
from ..services.order_manager import order_manager
from ..services.mode_service import mode_manager, feature_engine
from ..services.workstation_manager import workstation_manager

router = APIRouter(prefix="/api/orders", tags=["orders"])


# 주문 내용을 검증하고 대기열에 등록한다.
@router.post("")
async def create_order(payload: CreateOrderRequest):
    try:
        operation_mode = mode_manager.operation_mode
        driving_mode = "llm" if feature_engine("route", operation_mode) == "ai" else "standard"
        if (
            payload.operation_mode is not None
            and payload.operation_mode != operation_mode
            or payload.driving_mode is not None
            and payload.driving_mode != driving_mode
        ):
            raise ValueError("운영 모드가 변경되었습니다. 현재 모드를 확인하고 다시 등록하세요.")
        return order_manager.create_order(
            items=payload.items.model_dump(),
            total_quantity=payload.total_quantity,
            order_name=payload.order_name,
            driving_mode=driving_mode,
            selector=payload.selector,
            model=payload.model,
        )

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# 등록된 주문 목록을 반환한다.
@router.get("")
async def list_orders():
    orders = order_manager.list_orders()
    mode = mode_manager.mode
    return {
        "orders": orders,
        "workstations": workstation_manager.status(mode),
        "pickups": workstation_manager.pickup_status(mode, orders),
    }


# 실패한 주문을 다시 대기 상태로 돌린다.
@router.post("/{order_id}/retry")
async def retry_order(order_id: str):
    try:
        return order_manager.retry_order(order_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# 주문을 취소하고 점유 자원을 해제한다.
@router.delete("/{order_id}")
async def cancel_order(order_id: str):
    if not order_manager.cancel_order(order_id):
        raise HTTPException(status_code=404, detail="취소할 주문이 없습니다.")
    return {"order_id": order_id, "status": "CANCELLED"}
