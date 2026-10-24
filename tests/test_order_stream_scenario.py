"""자동 주문 공급의 일반 배차 연결·상한·중지·실기체 차단을 검증한다."""

import asyncio
import random
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.app.services import mode_service, order_manager, scenario_service, mock_data, simulation_order_runner, route_model_catalog
from backend.app.services.reservation import ReservationTable
from backend.simulation import order_stream_scenario as stream


@pytest.fixture
def environment(monkeypatch):
    manager = scenario_service.ScenarioManager()
    orders = order_manager.OrderManager()
    store = mock_data.MockFmsStore(ReservationTable())
    mode = SimpleNamespace(mode="simulation", operation_mode="ai")
    monkeypatch.setattr(scenario_service, "scenario_manager", manager)
    monkeypatch.setattr(scenario_service, "mode_manager", mode)
    monkeypatch.setattr(order_manager, "scenario_manager", manager)
    monkeypatch.setattr(order_manager, "order_manager", orders)
    monkeypatch.setattr(order_manager, "mode_manager", mode)
    monkeypatch.setattr(mode_service, "mode_manager", mode)
    monkeypatch.setattr(mock_data, "mock_fms", store)
    monkeypatch.setattr(route_model_catalog, "require_enabled_model", lambda *args: None)
    return manager, orders, store, mode


def test_random_orders_always_fit_the_total_capacity_and_reproduce():
    first, second = random.Random(42), random.Random(42)
    quantities = set()
    for _ in range(200):
        items = stream.random_items(first)
        assert items == stream.random_items(second)
        assert set(items) == set("ABCD")
        assert all(type(q) is int and q >= 0 for q in items.values())
        assert 1 <= sum(items.values()) <= 5
        quantities.add(sum(items.values()))
    assert quantities == {1, 2, 3, 4, 5}


@pytest.mark.parametrize("mode,operation", [("real", "ai"), ("simulation", "algorithm")])
def test_start_rejects_unsupported_modes_before_creating_orders(environment, mode, operation):
    manager, orders, _, current = environment
    current.mode, current.operation_mode = mode, operation
    with pytest.raises(ValueError, match="시뮬·AI"):
        manager.start("continuous_ai_orders")
    assert not orders.orders


def test_existing_orders_block_start(environment):
    manager, orders, _, _ = environment
    orders.create_order(dict(A=1, B=0, C=0, D=0), 1, selector="kev", model="kev-08b")
    with pytest.raises(ValueError, match="주문 또는"):
        manager.start("continuous_ai_orders")


def test_stream_uses_normal_order_queue_and_does_not_block_dispatch(environment, monkeypatch):
    manager, orders, _, _ = environment
    runner = SimpleNamespace(start=Mock(), start_recovery=Mock())
    monkeypatch.setattr(simulation_order_runner, "simulation_order_runner", runner)

    async def run():
        try:
            manager.start("continuous_ai_orders")
            await asyncio.sleep(0)
            assert len(orders.orders) == 3
            assert manager.active and not manager.blocks_order_processing
            with pytest.raises(ValueError, match="시나리오 실행"):
                manager.ensure_available()
            with pytest.raises(ValueError, match="시나리오 실행"):
                manager.start("continuous_ai_orders")
            assert all(row["status"] == "PENDING" for row in orders.list_orders())
            await orders.tick()
            assert runner.start.call_count == 3
            assert {row["robot_id"] for row in orders.list_orders()} == {"robot1", "robot2", "robot3"}
            assert all(row["status"] == "IN_PROGRESS" and row["model"] == "kev-08b" for row in orders.list_orders())
            manager.stop_order_supply()
            await asyncio.gather(manager._task, return_exceptions=True)
            assert manager.status()["state"] == "completed"
            assert all(row["status"] == "IN_PROGRESS" for row in orders.list_orders())
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_pending_limit_waits_then_resumes_and_stop_does_not_cancel_accepted_orders(environment, monkeypatch):
    manager, orders, _, _ = environment
    clock = [1.0]
    monkeypatch.setattr(stream, "monotonic", lambda: clock[0])

    async def run():
        try:
            manager.start("continuous_ai_orders")
            await asyncio.sleep(0)
            assert len(orders.orders) == 3
            clock[0] = 10
            await asyncio.sleep(.55)
            assert len(orders.orders) == 3  # 대기 상한이므로 추가 등록 없음
            row = next(iter(orders.orders.values()))
            row.update(status="COMPLETED", phase="COMPLETED")
            await asyncio.sleep(.55)
            assert len(orders.orders) == 4
            assert sum(row["status"] == "PENDING" for row in orders.list_orders()) == 3
            manager.stop_order_supply()
            await asyncio.gather(manager._task, return_exceptions=True)
            clock[0] = 100
            await asyncio.sleep(0)
            assert len(orders.orders) == 4
            assert sum(row["status"] == "PENDING" for row in orders.list_orders()) == 3
        finally:
            await manager.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("fault", ["mode", "order_failure"])
def test_mode_change_or_failed_order_stops_new_supply(environment, fault):
    manager, orders, _, mode = environment

    async def run():
        try:
            manager.start("continuous_ai_orders")
            await asyncio.sleep(0)
            if fault == "mode":
                mode.mode = "real"  # 외부 상태 변경을 흉내낸다. 실물 명령은 없다.
            else:
                next(iter(orders.orders.values()))["status"] = "FAILED"
            await asyncio.sleep(.55)
            assert manager.status()["state"] == "failed"
            assert len(orders.orders) == 3
            assert manager._task.done()
        finally:
            await manager.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("supply_stopped", [False, True])
def test_emergency_interrupt_cancels_only_demo_pending_orders(environment, supply_stopped):
    manager, orders, _, _ = environment

    async def run():
        try:
            manager.start("continuous_ai_orders")
            await asyncio.sleep(0)
            if supply_stopped:
                manager.stop_order_supply()
                await asyncio.gather(manager._task, return_exceptions=True)
            unrelated = orders.create_order(dict(A=1, B=0, C=0, D=0), 1, selector="kev", model="kev-08b")
            manager.interrupt()
            await asyncio.gather(manager._task, return_exceptions=True)
            assert manager.status()["state"] == "completed"
            ids = manager.status()["order_stream"]["order_ids"]
            assert all(orders.get_order(oid)["status"] == "CANCELLED" for oid in ids)
            assert orders.get_order(unrelated["order_id"])["status"] == "PENDING"
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_stopped_supply_status_tracks_later_completion_and_return_without_resupplying(environment):
    manager, orders, _, _ = environment

    async def run():
        try:
            manager.start("continuous_ai_orders")
            await asyncio.sleep(0)
            manager.stop_order_supply()
            await asyncio.gather(manager._task, return_exceptions=True)
            ids = manager.status()["order_stream"]["order_ids"]
            for oid in ids:
                orders.orders[oid].update(status="COMPLETED", phase="RETURNING")
            unrelated = orders.create_order(dict(A=1, B=0, C=0, D=0), 1, selector="kev", model="kev-08b")
            during_return = manager.status()
            assert during_return["order_stream"]["completed"] == 3
            assert during_return["order_stream"]["returning"] == 3
            assert during_return["order_stream"]["pending"] == 0
            assert "작업 완료 3" in during_return["message"] and "복귀 중 3" in during_return["message"]
            for oid in ids:
                orders.orders[oid]["phase"] = "COMPLETED"
            final = manager.status()
            assert final["state"] == "completed" and final["order_stream"]["returning"] == 0
            assert final["order_stream"]["generated"] == final["order_stream"]["completed"] == 3
            assert orders.get_order(unrelated["order_id"])["status"] == "PENDING"
            assert len(orders.orders) == 4 and manager._task.done()
            assert manager.status()["message"] == final["message"]  # 반복 조회에 문구가 누적되지 않음
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_failed_supply_keeps_failure_reason_while_showing_later_order_states(environment):
    manager, orders, _, mode = environment

    async def run():
        try:
            manager.start("continuous_ai_orders")
            await asyncio.sleep(0)
            mode.mode = "real"
            await asyncio.sleep(.55)
            assert manager.status()["state"] == "failed"
            for row in orders.orders.values():
                row.update(status="CANCELLED", phase="CANCELLED")
            status = manager.status()
            assert status["state"] == "failed" and "모드가 변경" in status["message"]
            assert status["order_stream"]["cancelled"] == 3 and "취소 3" in status["message"]
            assert len(orders.orders) == 3
        finally:
            await manager.shutdown()

    asyncio.run(run())
