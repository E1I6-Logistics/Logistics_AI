"""실제 배차의 진행 주문 점유에 가상 로봇 주문이 섞이지 않는지 확인한다."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from backend.app.models.robot import RobotState
from backend.app.services import order_dispatcher as dispatch, order_manager as orders
from backend.app.services.fleet_manager import fleet_manager
from backend.app.services.mode_service import mode_manager
from tests.test_charging_docking import docking, finish


@pytest.mark.parametrize("state", ["IDLE", "CHARGING"])
@pytest.mark.parametrize("other_order_mode", [None, "simulation", "real"])
def test_real_dispatch_only_counts_active_orders_in_real_mode(docking, monkeypatch, state, other_order_mode):
    ctx = docking
    if state == "CHARGING":
        finish(ctx)
    else:
        robot = ctx.fleet.get_robot("robot1")
        robot.navigation_type = robot.goal_node = robot.route = None
        robot.set_state(RobotState.IDLE)
    for rid in ("robot2", "robot3"):
        ctx.fleet.get_robot(rid).set_connected(False)
    monkeypatch.setattr(fleet_manager, "get_all_robots", ctx.fleet.get_all_robots)
    monkeypatch.setattr(fleet_manager, "get_robot", ctx.fleet.get_robot)
    monkeypatch.setattr(dispatch, "ros_gateway", ctx.gateway)
    monkeypatch.setattr(dispatch, "monotonic", lambda: ctx.clock[0])
    monkeypatch.setattr(orders, "ros_gateway", ctx.gateway)
    monkeypatch.setattr(mode_manager, "_mode", "real")
    monkeypatch.setattr(mode_manager, "_operation_mode", "algorithm")
    manager = orders.OrderManager()
    # 배차 결정까지 실제 코드를 실행하고, 이후 장비 주행 단계는 보내지 않는다.
    manager._tick_real = AsyncMock()
    manager._tick_real_returns = AsyncMock()
    if other_order_mode is not None:
        manager.orders["previous-order"] = dict(
            order_id="previous-order", mode=other_order_mode,
            status="IN_PROGRESS", robot_id="robot1",
        )
    order = manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
    asyncio.run(manager.tick())
    row = manager.get_order(order["order_id"])
    if other_order_mode == "real":
        assert row["robot_id"] is None
        assert row["status"] == "PENDING"
    else:
        assert row["robot_id"] == "robot1"
        assert row["status"] == "IN_PROGRESS"
    if other_order_mode is not None:
        assert manager.orders["previous-order"]["status"] == "IN_PROGRESS"
