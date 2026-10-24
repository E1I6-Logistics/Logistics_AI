"""픽업 해제 기준: 앞 주문 완료가 아니라 현재 픽업 요청·실제 점유."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from backend.app.services.pickup_queue import earlier_pickup_request, requested_pickup_node
from backend.app.services import order_manager, simulation_order_runner, mock_data


def row(order_id, robot, phase, pickups=("5",), index=0):
    return dict(order_id=order_id, robot_id=robot, phase=phase, status="IN_PROGRESS",
                mode="simulation", pickup_nodes=list(pickups), pickup_index=index)


@pytest.mark.parametrize("phase,blocked", [
    ("MOVING_TO_PICKUP", True), ("WORKING_AT_PICKUP", True), ("WAITING_PICKUP", True),
    ("MOVING_TO_WORKSTATION", False), ("WORKING_AT_WORKSTATION", False),
    ("WAITING_WORKSTATION", False), ("RETURNING", False),
])
def test_only_current_pickup_phases_hold_turn(phase, blocked):
    first, second = row("first", "robot1", phase), row("second", "robot2", "WAITING_PICKUP")
    assert earlier_pickup_request([first, second], second, "5") == blocked


def test_first_pickup_does_not_claim_a_future_pickup():
    first = row("first", "robot1", "WORKING_AT_PICKUP", ("5", "6"))
    second = row("second", "robot2", "WAITING_PICKUP", ("6",))
    assert not earlier_pickup_request([first, second], second, "6")
    first["pickup_index"] = 1
    assert earlier_pickup_request([first, second], second, "6")
    assert not earlier_pickup_request([first, second], second, "5")


def test_cancelled_or_completed_orders_do_not_hold_turn():
    first, second = row("first", "robot1", "WORKING_AT_PICKUP"), row("second", "robot2", "WAITING_PICKUP")
    for status in ("FAILED", "CANCELLED", "COMPLETED"):
        first["status"] = status
        assert not earlier_pickup_request([first, second], second, "5")


def test_actual_occupancy_must_clear_even_after_pickup_phase_finished(monkeypatch):
    first = row("first", "robot1", "MOVING_TO_WORKSTATION")
    second = row("second", "robot2", "WAITING_PICKUP")
    current = order_manager.OrderManager()
    current.orders = {"first": first, "second": second}
    monkeypatch.setattr(order_manager, "order_manager", current)
    locations = [dict(robot_id="robot1", occupied_node="5"), dict(robot_id="robot2", occupied_node="1")]
    monkeypatch.setattr(simulation_order_runner, "mock_fms", SimpleNamespace(robot_snapshots=lambda mode: deepcopy(locations)))
    runner = simulation_order_runner.SimulationOrderRunner()

    async def run():
        task = asyncio.create_task(runner._wait_for_pickup_turn(second, "5"))
        await asyncio.sleep(.02)
        assert not task.done()
        locations[0]["occupied_node"] = None  # 앞 로봇이 N5에서 엣지로 실제 출발
        assert await asyncio.wait_for(task, .5) is True
        assert first["status"] == "IN_PROGRESS"

    asyncio.run(run())
