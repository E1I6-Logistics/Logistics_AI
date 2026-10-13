from typing import Literal

from pydantic import BaseModel


class OrderItems(BaseModel):
    A: int
    B: int
    C: int
    D: int


class CreateOrderRequest(BaseModel):
    order_name: str | None = None
    items: OrderItems
    total_quantity: int
    driving_mode: Literal["standard", "llm"] = "standard"
    selector: str | None = None
    model: str | None = None
