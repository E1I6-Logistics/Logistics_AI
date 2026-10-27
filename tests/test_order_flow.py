"""주문 실행의 순서와 작업대 선택을 검증한다."""

import asyncio
import pytest
from time import monotonic

from backend.app.services.mock_data import mock_fms
from backend.app.services.mode_service import mode_manager
from backend.app.services.order_manager import order_manager
from backend.app.services.simulation_order_runner import simulation_order_runner
from backend.app.services.workstation_manager import workstation_manager
from backend.app.services.kev_mapf.kev_workstation_dispatcher import kev_workstation_dispatcher


@pytest.mark.parametrize("previous_phase,previous_pickups", [
    ("WORKING_AT_WORKSTATION", ["5"]),
    ("MOVING_TO_PICKUP", ["6", "5"]),
])
def test_pickup_admission_does_not_wait_for_unrelated_work(monkeypatch, previous_phase, previous_pickups):
    _reset(monkeypatch)
    previous = dict(order_id="earlier", mode="simulation", status="IN_PROGRESS",
                    phase=previous_phase, pickup_nodes=previous_pickups, pickup_index=0)
    current = dict(order_id="current", robot_id="robot2", pickup_nodes=["5"], pickup_index=0)
    monkeypatch.setattr(order_manager, "orders", {"earlier": previous, "current": current})
    monkeypatch.setattr(order_manager, "is_current", lambda *_: True)
    monkeypatch.setattr(mock_fms, "robot_snapshots", lambda _: [])

    async def scenario():
        assert await asyncio.wait_for(simulation_order_runner._wait_for_pickup_turn(current), .1)

    asyncio.run(scenario())


def test_pickup_wait_ends_when_previous_robot_vacates_node(monkeypatch):
    _reset(monkeypatch)
    previous = dict(order_id="earlier", mode="simulation", status="IN_PROGRESS",
                    phase="MOVING_TO_WORKSTATION", pickup_nodes=["5"], pickup_index=0)
    current = dict(order_id="current", robot_id="robot2", pickup_nodes=["5"], pickup_index=0)
    monkeypatch.setattr(order_manager, "orders", {"earlier": previous, "current": current})
    monkeypatch.setattr(order_manager, "is_current", lambda *_: True)
    monkeypatch.setattr(order_manager, "set_phase", lambda *_: None)
    snapshots = [{"robot_id": "robot1", "occupied_node": "5"}]
    monkeypatch.setattr(mock_fms, "robot_snapshots", lambda _: snapshots)

    async def scenario():
        task = asyncio.create_task(simulation_order_runner._wait_for_pickup_turn(current))
        await asyncio.sleep(.01)
        assert not task.done()
        snapshots[0]["occupied_node"] = None
        assert await asyncio.wait_for(task, .3)
        assert previous["status"] == "IN_PROGRESS"

    asyncio.run(scenario())


# 테스트 사이에 공유 상태를 초기화한다.
def _reset(monkeypatch):
    # 기존 경로/주문 테스트는 배차 선택과 분리한다. KEV 통합은 별도 테스트에서 활성화한다.
    monkeypatch.setenv("FMS_SIM_KEV_DISPATCH_ENABLED", "0")
    monkeypatch.setenv("FMS_REAL_KEV_DISPATCH_ENABLED", "0")
    order_manager._robot_dispatcher.discard()
    monkeypatch.setenv("KEV_WORKSTATION_ENABLED", "0")
    mode_manager.set_mode("simulation", "algorithm")
    mock_fms.reset()
    order_manager.orders.clear()
    order_manager.returning.clear()
    order_manager.recovering.clear()
    order_manager._return_started.clear()
    order_manager._route_tasks.clear()
    workstation_manager._assigned.clear()
    workstation_manager._assignment_history.clear()
    kev_workstation_dispatcher._selector = None
    simulation_order_runner.tasks.clear()
    simulation_order_runner.work_seconds = 0.01
    visits = []

    # 테스트 로봇을 지정한 노드에 도착시킨다.
    def arrive(robot_id, node_id, *, required_path=None):
        node = str(node_id)
        visits.append((robot_id, node, required_path))
        with mock_fms._lock:
            robot = mock_fms._robots[robot_id]
            robot.update(
                occupied_node=node,
                current_node=node,
                occupied_edge=None,
                goal_node=None,
                route=None,
                status="IDLE",
                stop_requested=False,
            )
        return {"status": "SUCCESS"}

    monkeypatch.setattr(mock_fms, "navigate_to_node", arrive)
    return visits


# 시뮬레이션이 픽업대와 작업대를 거쳐 복귀하는지 검증한다.
def test_simulation_pickups_then_workstation_then_return(monkeypatch):
    visits = _reset(monkeypatch)

    # 주문 흐름을 재현하는 테스트 시나리오를 실행한다.
    async def scenario():
        order = order_manager.create_order(items={"A": 1, "B": 0, "C": 2, "D": 0}, total_quantity=3)
        assert order["workstation_node"] is None
        await order_manager.tick()
        assert order_manager.get_order(order["order_id"])["robot_id"] == "robot1"
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))
        finished = order_manager.get_order(order["order_id"])
        assert finished["status"] == "COMPLETED"
        assert finished["workstation_node"] == "3"
        assert [node for _, node, _ in visits] == ["5", "6", "3", "0"]

    asyncio.run(scenario())


# 네 가지 운영 모드가 각 기능 정책을 따르는지 검증한다.
@pytest.mark.parametrize("environment", ["simulation", "real"])
@pytest.mark.parametrize("operation", ["algorithm", "ai"])
def test_four_operating_modes_use_feature_policies(monkeypatch, environment, operation):
    _reset(monkeypatch)
    monkeypatch.setenv("FMS_SIM_KEV_DISPATCH_ENABLED", "1")
    from backend.app.routers.mode import ModeRequest, get_mode, set_mode
    from backend.app.routers.orders import create_order
    from backend.app.schemas.order import CreateOrderRequest
    from backend.app.services import route_model_catalog

    monkeypatch.setattr(route_model_catalog, "require_enabled_model", lambda *_: None)

    # 주문 흐름을 재현하는 테스트 시나리오를 실행한다.
    async def scenario():
        result = await set_mode(ModeRequest(mode=environment, operation_mode=operation))
        assert (result["mode"], result["operation_mode"]) == (environment, operation)
        assert (await get_mode())["operation_mode"] == operation
        assert result["feature_engines"] == {
            "route": "ai" if operation == "ai" else "algorithm",
            "dispatch": "ai" if operation == "ai" and environment == "simulation" else "algorithm",
            "workstation": "ai" if operation == "ai" else "algorithm",
        }
        order = await create_order(
            CreateOrderRequest(
                items={"A": 1, "B": 0, "C": 0, "D": 0},
                total_quantity=1,
                operation_mode=operation,
                selector="laya",
                model="test",
            )
        )
        assert order["operation_mode"] == operation
        assert order["driving_mode"] == ("llm" if operation == "ai" else "standard")
        assert order["mode"] == environment
        assert workstation_manager.choose(order["order_id"], environment, operation) == "3"

    try:
        asyncio.run(scenario())
    finally:
        mode_manager.set_mode("simulation", "algorithm")


# 대기 주문이나 오래된 화면 정보로 모드 전환을 막는지 검증한다.
def test_operation_switch_rejects_active_queue_and_stale_form(monkeypatch):
    _reset(monkeypatch)
    from fastapi import HTTPException
    from backend.app.routers.mode import ModeRequest, set_mode
    from backend.app.routers.orders import create_order
    from backend.app.schemas.order import CreateOrderRequest

    # 주문 흐름을 재현하는 테스트 시나리오를 실행한다.
    async def scenario():
        order = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        with pytest.raises(HTTPException) as blocked:
            await set_mode(ModeRequest(mode="simulation", operation_mode="ai"))
        assert blocked.value.status_code == 409
        assert mode_manager.operation_mode == "algorithm"
        order_manager.cancel_order(order["order_id"])
        await set_mode(ModeRequest(mode="simulation", operation_mode="ai"))
        with pytest.raises(HTTPException) as stale:
            await create_order(
                CreateOrderRequest(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1, operation_mode="algorithm")
            )
        assert stale.value.status_code == 400
        assert len(order_manager.orders) == 1

    try:
        asyncio.run(scenario())
    finally:
        mode_manager.set_mode("simulation", "algorithm")


# 점유된 작업대 노드가 해제될 때까지 기다리는지 검증한다.
def test_workstation_waits_until_occupied_node_is_released(monkeypatch):
    visits = _reset(monkeypatch)
    mock_fms._robots["robot2"]["occupied_node"] = "3"
    mock_fms._robots["robot3"]["occupied_node"] = "4"

    # 주문 흐름을 재현하는 테스트 시나리오를 실행한다.
    async def scenario():
        order = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        await order_manager.tick()
        await asyncio.sleep(0.05)
        waiting = order_manager.get_order(order["order_id"])
        assert waiting["phase"] == "WAITING_WORKSTATION"
        assert waiting["workstation_node"] is None
        assert [node for _, node, _ in visits] == ["5"]
        mock_fms._robots["robot3"]["occupied_node"] = "2"
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))
        assert order_manager.get_order(order["order_id"])["workstation_node"] == "4"
        assert [node for _, node, _ in visits] == ["5", "4", "0"]

    asyncio.run(scenario())


# 작업대가 중복 배정되지 않는지 검증한다.
def test_workstation_assignment_is_unique(monkeypatch):
    _reset(monkeypatch)
    assert workstation_manager.choose("first", "simulation") == "3"
    assert workstation_manager.choose("second", "simulation") == "4"
    assert workstation_manager.choose("third", "simulation") is None
    workstation_manager.release("first")
    assert workstation_manager.choose("third", "simulation") == "3"


def test_ai_workstation_dispatch_uses_kev_and_reserves_one_free_node(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setenv("KEV_WORKSTATION_ENABLED", "1")
    calls = []

    class FakeKevSelector:
        def select_choice(self, **kwargs):
            calls.append(kwargs)
            return {"choice": "4"}

    kev_workstation_dispatcher._selector = FakeKevSelector()

    async def scenario():
        chosen = await workstation_manager.choose_with_kev("kev-order", "simulation", "ai", "robot1", "5")
        assert chosen == "4"
        assert workstation_manager._assigned == {"4": "kev-order"}

    asyncio.run(scenario())
    assert len(calls) == 1
    assert set(calls[0]["candidates"]) == {"3", "4"}


# 주문 완료 후 남은 대기 주문을 배차하는지 검증한다.
def test_queue_dispatches_remaining_orders_after_completion(monkeypatch):
    _reset(monkeypatch)

    # 주문 흐름을 재현하는 테스트 시나리오를 실행한다.
    async def scenario():
        for _ in range(5):
            order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        await order_manager.tick()
        assert sum(row["status"] == "PENDING" for row in order_manager.list_orders()) == 2
        for _ in range(100):
            await asyncio.sleep(0.01)
            await order_manager.tick()
            if all(row["status"] == "COMPLETED" for row in order_manager.list_orders()):
                break
        assert all(row["status"] == "COMPLETED" for row in order_manager.list_orders())
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))

    asyncio.run(scenario())


# 실패 로봇의 복귀 중 다른 로봇이 대기 주문을 처리하는지 검증한다.
def test_failed_robot_recovers_while_another_robot_handles_queue(monkeypatch):
    _reset(monkeypatch)

    # 주문 흐름을 재현하는 테스트 시나리오를 실행한다.
    async def scenario():
        first = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        await order_manager.tick()
        order_manager.fail(first["order_id"], "경로 요청 실패")
        second = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        await order_manager.tick()
        assert order_manager.get_order(first["order_id"])["status"] == "FAILED"
        assert order_manager.get_order(second["order_id"])["robot_id"] == "robot2"
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))
        assert order_manager.get_order(second["order_id"])["status"] == "COMPLETED"
        assert order_manager.get_order(first["order_id"])["recovery_status"] == "RETURNED"
        assert order_manager.recovering == {}

    asyncio.run(scenario())


# 오래된 실패 주문을 취소해도 새 주문을 멈추지 않는지 검증한다.
def test_cancelling_old_failure_does_not_stop_new_order(monkeypatch):
    _reset(monkeypatch)

    # 주문 흐름을 재현하는 테스트 시나리오를 실행한다.
    async def scenario():
        first = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        await order_manager.tick()
        order_manager.fail(first["order_id"], "경로 요청 실패")
        await order_manager.tick()
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))
        second = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        await order_manager.tick()
        assert order_manager.get_order(second["order_id"])["robot_id"] == "robot1"
        stopped = []
        original_stop = mock_fms.stop_robot

        # 테스트에서 로봇 정지 요청을 기록한다.
        def record_stop(robot_id):
            stopped.append(robot_id)
            return original_stop(robot_id)

        monkeypatch.setattr(mock_fms, "stop_robot", record_stop)
        assert order_manager.cancel_order(first["order_id"])
        assert stopped == []
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))
        assert order_manager.get_order(second["order_id"])["status"] == "COMPLETED"

    asyncio.run(scenario())


# 간선에서 경로가 끝나도 시뮬레이션이 재시도하는지 검증한다.
def test_simulation_retries_after_traffic_route_ends_on_edge(monkeypatch):
    _reset(monkeypatch)
    calls = []

    # 테스트에서 한 번 중단되는 상황을 재현한다.
    def interrupted_once(robot_id, node_id, *, required_path=None):
        calls.append(str(node_id))
        with mock_fms._lock:
            robot = mock_fms._robots[robot_id]
            if len(calls) == 1:
                robot.update(
                    occupied_node=None,
                    occupied_edge="edge_0_7",
                    goal_node=None,
                    route=None,
                    status="IDLE",
                    stop_requested=False,
                )
            else:
                node = str(node_id)
                robot.update(
                    occupied_node=node,
                    current_node=node,
                    occupied_edge=None,
                    goal_node=None,
                    route=None,
                    status="IDLE",
                    stop_requested=False,
                )
        return {"status": "SUCCESS"}

    monkeypatch.setattr(mock_fms, "navigate_to_node", interrupted_once)

    # 주문 흐름을 재현하는 테스트 시나리오를 실행한다.
    async def scenario():
        order = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        await order_manager.tick()
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))
        assert calls[:2] == ["5", "5"]
        assert order_manager.get_order(order["order_id"])["status"] == "COMPLETED"

    asyncio.run(scenario())


# 실제 모드에서 주행 완료 후 다음 주문을 배차하는지 검증한다.
def test_real_dispatch_waits_for_navigation_to_finish(monkeypatch):
    from backend.app.models.robot import Robot, RobotState
    from backend.app.ros2.ros_gateway import ros_gateway
    from backend.app.services.fleet_manager import fleet_manager
    from backend.app.services.order_dispatcher import available_robots

    robot = Robot("robot1")
    robot.update_battery(80, voltage=12)
    robot.set_connected(True)
    robot.update_pose(0, 0, 0)
    robot.occupied_node = "0"
    robot.set_state(RobotState.IDLE)
    monkeypatch.setattr(fleet_manager, "get_all_robots", lambda: [robot])
    monkeypatch.setattr(fleet_manager, "get_robot", lambda _id: robot)
    monkeypatch.setattr(ros_gateway, "navigation_active", lambda _id: True)
    assert available_robots("real", set()) == []
    monkeypatch.setattr(ros_gateway, "navigation_active", lambda _id: False)
    assert available_robots("real", set()) == ["robot1"]


# 앞선 픽업 주문이 대기해도 복귀 중인 로봇을 가로채지 않는지 검증한다.
def test_returning_robot_is_not_preempted_while_earlier_pickup_waits(monkeypatch):
    _reset(monkeypatch)

    # 주문 흐름을 재현하는 테스트 시나리오를 실행한다.
    async def scenario():
        orders = [
            order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1) for _ in range(4)
        ]
        await order_manager.tick()
        first = order_manager.orders[orders[0]["order_id"]]
        first["status"] = "COMPLETED"
        first["phase"] = "RETURNING"
        order_manager.returning["robot1"] = first["order_id"]
        with mock_fms._lock:
            mock_fms._robots["robot1"].update(occupied_node="3", current_node="3", goal_node="0", status="NAVIGATING")
        await order_manager.tick()
        assert order_manager.get_order(orders[3]["order_id"])["status"] == "PENDING"
        await simulation_order_runner.shutdown()

    asyncio.run(scenario())


# 모의 작업 중 취소하면 기존 타이머가 무효화되는지 검증한다.
def test_cancel_during_mock_work_invalidates_timer(monkeypatch):
    visits = _reset(monkeypatch)
    simulation_order_runner.work_seconds = 0.1

    # 주문 흐름을 재현하는 테스트 시나리오를 실행한다.
    async def scenario():
        order = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        await order_manager.tick()
        await asyncio.sleep(0.02)
        assert order_manager.get_order(order["order_id"])["phase"] == "WORKING_AT_PICKUP"
        snapshot = mock_fms.robot_snapshot("robot1", "simulation")
        assert snapshot["occupied_node"] == "5"
        assert snapshot["stop_requested"] is True  # excluded from automatic yielding
        assert order_manager.cancel_order(order["order_id"])
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))
        assert order_manager.get_order(order["order_id"])["status"] == "CANCELLED"
        assert [node for _, node, _ in visits] == ["5"]

    asyncio.run(scenario())


# 대기 주문을 수동 취소하면 로봇 주행 없이 목록 상태만 바뀌는지 검증한다.
def test_cancel_pending_order_does_not_assign_robot(monkeypatch):
    _reset(monkeypatch)
    order = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
    assert order_manager.cancel_order(order["order_id"])
    asyncio.run(order_manager.tick())
    assert order_manager.get_order(order["order_id"])["status"] == "CANCELLED"
    assert order_manager.recovering == {}


# 픽업대에서 주문을 취소해도 적재 상태와 관계없이 스테이션으로 복귀하는지 검증한다.
def test_cancel_active_simulation_order_returns_to_station(monkeypatch):
    visits = _reset(monkeypatch)
    simulation_order_runner.work_seconds = 0.1

    async def scenario():
        order = order_manager.create_order(items={"A": 2, "B": 0, "C": 0, "D": 0}, total_quantity=2)
        await order_manager.tick()
        await asyncio.sleep(0.02)
        assert order_manager.get_order(order["order_id"])["phase"] == "WORKING_AT_PICKUP"
        assert order_manager.cancel_order(order["order_id"])
        assert order_manager.recovering["robot1"] == order["order_id"]
        queued = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        await order_manager.tick()
        assert order_manager.get_order(queued["order_id"])["robot_id"] != "robot1"
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))
        cancelled = order_manager.get_order(order["order_id"])
        assert cancelled["status"] == cancelled["phase"] == "CANCELLED"
        assert cancelled["recovery_status"] == "RETURNED"
        assert ("robot1", "0", None) in visits
        assert order_manager.recovering == {}

    asyncio.run(scenario())


# 실제 주문 취소는 기존 작업 종료 확인 후 알고리즘 경로로 복귀하는지 검증한다.
def test_cancel_active_real_order_returns_after_stop(monkeypatch):
    _reset(monkeypatch)
    from backend.app.models.robot import Robot, RobotState
    from backend.app.services import order_manager as order_module
    from backend.app.services.fleet_manager import fleet_manager

    robot = Robot("robot1")
    robot.set_connected(True)
    robot.update_pose(0, 0, 0)
    robot.current_node = robot.occupied_node = "5"
    monkeypatch.setattr(fleet_manager, "get_robot", lambda _id: robot)
    monkeypatch.setattr(fleet_manager, "get_all_robots", lambda: [robot])
    callbacks = []
    routes = []
    monkeypatch.setattr(order_module.ros_gateway, "cancel_current_work", lambda _id, callback: callbacks.append(callback))
    monkeypatch.setattr(order_module.ros_gateway, "navigation_active", lambda _id: False)

    async def route(row, target, start, valid=None):
        assert valid()
        routes.append((row["operation_mode"], target, start))
        robot.current_node = robot.occupied_node = target
        robot.set_state(RobotState.IDLE)

    monkeypatch.setattr(order_module, "dispatch_order_route", route)
    mode_manager.set_mode("real")
    try:
        order = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        row = order_manager.orders[order["order_id"]]
        row.update(status="IN_PROGRESS", phase="WORKING_AT_PICKUP", robot_id="robot1")
        robot.assign_order(order["order_id"], row["items"], 1, ["5"], None)
        assert order_manager.cancel_order(order["order_id"])
        assert row["recovery_status"] == "PENDING"
        assert order_manager.recovering["robot1"] == order["order_id"]
        callbacks.pop()()

        async def finish_return():
            await order_manager._tick_real_returns()
            await asyncio.gather(*list(order_manager._route_tasks.values()))
            await order_manager._tick_real_returns()

        asyncio.run(finish_return())
        assert routes == [("algorithm", "0", "5")]
        assert row["status"] == row["phase"] == "CANCELLED"
        assert row["recovery_status"] == "RETURNED"
        assert order_manager.recovering == {}
    finally:
        mode_manager.set_mode("simulation")


# 복귀 중 로봇을 수동 정지해도 주문 취소 상태가 완료로 바뀌지 않는지 검증한다.
def test_stopping_cancelled_return_keeps_order_cancelled(monkeypatch):
    _reset(monkeypatch)
    order = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
    row = order_manager.orders[order["order_id"]]
    row.update(status="CANCELLED", phase="CANCELLED", robot_id="robot1", recovery_status="RETURNING")
    order_manager.returning["robot1"] = order["order_id"]
    order_manager.recovering["robot1"] = order["order_id"]
    order_manager.cancel_robot("robot1", "simulation")
    assert row["status"] == row["phase"] == "CANCELLED"
    assert row["recovery_status"] == "STOPPED"


# 픽업 후 실패 시 점유를 풀고 재시도에 다른 로봇을 우선하는지 검증한다.
def test_failure_after_pickup_clears_node_and_retry_prefers_another_robot(monkeypatch):
    _reset(monkeypatch)
    from backend.app.services import simulation_order_runner as runner_module

    original_dispatch = runner_module.dispatch_order_route
    fail_once = True

    # 테스트에서 작업대 실패를 재현한다.
    async def fail_workstation(order, node, current_node, valid=None):
        nonlocal fail_once
        if str(node) == "3" and fail_once:
            fail_once = False
            raise RuntimeError("테스트: 경로 모델 응답 실패")
        return await original_dispatch(order, node, current_node, valid=valid)

    monkeypatch.setattr(runner_module, "dispatch_order_route", fail_workstation)

    # 주문 흐름을 재현하는 테스트 시나리오를 실행한다.
    async def scenario():
        order = order_manager.create_order(items={"A": 2, "B": 0, "C": 0, "D": 0}, total_quantity=2)
        order_id = order["order_id"]
        await order_manager.tick()
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))
        failed = order_manager.get_order(order_id)
        assert failed["status"] == "FAILED"
        assert failed["failed_phase"] == "MOVING_TO_WORKSTATION"
        assert failed["error"] == "테스트: 경로 모델 응답 실패"
        assert mock_fms.robot_snapshot("robot1", "simulation")["occupied_node"] == "5"
        assert workstation_manager._assigned == {}
        await order_manager.tick()
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))
        assert mock_fms.robot_snapshot("robot1", "simulation")["occupied_node"] == "0"
        assert order_manager.get_order(order_id)["recovery_status"] == "RETURNED"
        order_manager.retry_order(order_id)
        await order_manager.tick()
        assert order_manager.get_order(order_id)["robot_id"] == "robot2"
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))
        retried = order_manager.get_order(order_id)
        assert retried["status"] == "COMPLETED"
        assert len(retried["failures"]) == 1

    asyncio.run(scenario())


# 전체 정지가 실패 복구 작업도 취소하는지 검증한다.
def test_stop_all_cancels_failure_recovery(monkeypatch):
    _reset(monkeypatch)
    from backend.app.routers.commands import stop_all_robots

    # 주문 흐름을 재현하는 테스트 시나리오를 실행한다.
    async def scenario():
        order = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        await order_manager.tick()
        order_manager.fail(order["order_id"], "테스트 실패")
        assert order_manager.recovering
        await stop_all_robots()
        await order_manager.tick()
        await simulation_order_runner.shutdown()
        assert order_manager.recovering == {}
        assert order_manager.get_order(order["order_id"])["recovery_status"] == "STOPPED"
        assert mock_fms.robot_snapshot("robot1", "simulation")["goal_node"] is None

    asyncio.run(scenario())


# 복구 중인 로봇이 충전소 복귀 전까지 배차되지 않는지 검증한다.
def test_failed_recovery_stays_unavailable_until_station_return(monkeypatch):
    _reset(monkeypatch)
    from backend.app.services import simulation_order_runner as runner_module

    # 테스트에서 사용 불가한 경로를 반환한다.
    async def unavailable_route(*_args, **_kwargs):
        raise ValueError("복귀 경로 없음")

    # 주문 흐름을 재현하는 테스트 시나리오를 실행한다.
    async def scenario():
        first = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        await order_manager.tick()
        await asyncio.sleep(0)
        order_manager.fail(first["order_id"], "픽업 작업 실패")
        monkeypatch.setattr(runner_module, "dispatch_order_route", unavailable_route)
        await order_manager.tick()
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))
        assert order_manager.get_order(first["order_id"])["recovery_status"] == "BLOCKED"
        assert mock_fms.robot_snapshot("robot1", "simulation")["occupied_node"] == "5"
        second = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        await order_manager.tick()
        assert order_manager.get_order(second["order_id"])["robot_id"] == "robot2"
        mock_fms.navigate_to_node("robot1", "0")
        await order_manager.tick()
        assert order_manager.get_order(first["order_id"])["recovery_status"] == "RETURNED"
        await simulation_order_runner.shutdown()

    asyncio.run(scenario())


# 픽업대 상태에 점유 주문과 대기 주문을 표시하는지 검증한다.
def test_pickup_status_shows_occupant_order_and_waiting_orders(monkeypatch):
    _reset(monkeypatch)
    simulation_order_runner.work_seconds = 0.1

    # 주문 흐름을 재현하는 테스트 시나리오를 실행한다.
    async def scenario():
        for _ in range(2):
            order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        await order_manager.tick()
        await asyncio.sleep(0.02)
        status = workstation_manager.pickup_status("simulation", order_manager.list_orders())
        assert status["5"]["status"] == "OCCUPIED"
        assert status["5"]["robot_ids"] == ["robot1"]
        assert status["5"]["order_name"] == "ORDER-001"
        assert status["5"]["phase"] == "WORKING_AT_PICKUP"
        assert status["5"]["waiting_orders"] == ["ORDER-002"]
        assert status["6"]["status"] == "FREE"
        await simulation_order_runner.shutdown()

    asyncio.run(scenario())


# 작업대 선택 후에만 AI 경로를 요청하는지 검증한다.
def test_ai_route_is_requested_only_after_workstation_selection(monkeypatch):
    visits = _reset(monkeypatch)
    # This test isolates order/preview timing; MAPF admission has dedicated tests.
    monkeypatch.setenv("FMS_SIM_MAPF_ENABLED", "0")
    from backend.app.services import route_model_catalog, order_route_dispatcher

    monkeypatch.setattr(route_model_catalog, "require_enabled_model", lambda *_: None)

    # 테스트에서 경로 선택 결과를 미리 계산한다.
    async def preview(_function, _graph, start, target, **_options):
        snapshot = mock_fms.robot_snapshot("robot1", "simulation")
        assert snapshot["occupied_node"] == str(start)
        assert snapshot["stop_requested"] is True
        assert snapshot["route"] is None
        await asyncio.sleep(0.01)
        return {"path": [str(start), str(target)]}

    monkeypatch.setattr(order_route_dispatcher.asyncio, "to_thread", preview)
    mock_fms._robots["robot2"]["occupied_node"] = "3"
    mock_fms._robots["robot3"]["occupied_node"] = "4"

    # 주문 흐름을 재현하는 테스트 시나리오를 실행한다.
    async def scenario():
        order = order_manager.create_order(
            items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1, driving_mode="llm", selector="laya", model="test"
        )
        await order_manager.tick()
        await asyncio.sleep(0.05)
        assert [node for _, node, _ in visits] == ["5"]
        mock_fms._robots["robot2"]["occupied_node"] = "1"
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))
        assert [node for _, node, _ in visits] == ["5", "3", "0"]
        assert all(path is not None for _, _, path in visits)
        assert order_manager.get_order(order["order_id"])["status"] == "COMPLETED"

    asyncio.run(scenario())


# 잘못된 AI 경로는 주행하지 않고 실패 로봇을 복귀시키는지 검증한다.
def test_invalid_ai_route_is_not_driven_and_failure_returns_robot(monkeypatch):
    visits = _reset(monkeypatch)
    monkeypatch.setenv("FMS_SIM_MAPF_ENABLED", "0")  # Isolate preview failure handling.
    from backend.app.services import route_model_catalog, order_route_dispatcher

    monkeypatch.setattr(route_model_catalog, "require_enabled_model", lambda *_: None)

    # 테스트에서 경로 선택 결과를 미리 계산한다.
    async def preview(_function, _graph, start, target, **_options):
        if str(target) == "3":
            raise ValueError("순환 방지 조건을 만족하는 이동 후보가 없습니다: current=10")
        return {"path": [str(start), str(target)]}

    monkeypatch.setattr(order_route_dispatcher.asyncio, "to_thread", preview)

    # 주문 흐름을 재현하는 테스트 시나리오를 실행한다.
    async def scenario():
        order = order_manager.create_order(
            items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1, driving_mode="llm", selector="laya", model="test"
        )
        await order_manager.tick()
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))
        failed = order_manager.get_order(order["order_id"])
        assert failed["status"] == "FAILED"
        assert "AI 경로 생성 실패(N5 → N3)" in failed["error"]
        assert failed["failure_robot_state"]["occupied_node"] == "5"
        assert [node for _, node, _ in visits] == ["5"]
        await order_manager.tick()
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))
        assert visits[-1] == ("robot1", "0", None)
        assert order_manager.get_order(order["order_id"])["recovery_status"] == "RETURNED"

    asyncio.run(scenario())


# 실제 MQTT 작업에서 마지막 픽업 후 작업대를 선택하는지 검증한다.
def test_real_mqtt_uses_same_workstation_selection_after_last_pickup(monkeypatch):
    _reset(monkeypatch)
    from types import SimpleNamespace
    from backend.app.models.robot import Robot, RobotState
    from backend.app.services import order_manager as order_module
    from backend.app.services.fleet_manager import fleet_manager

    robot = Robot("robot1")
    robot.set_connected(True)
    robot.update_pose(0, 0, 0)
    robot.current_node = robot.occupied_node = "5"
    monkeypatch.setattr(fleet_manager, "get_robot", lambda _id: robot)
    monkeypatch.setattr(fleet_manager, "get_all_robots", lambda: [robot])
    moves = []

    # 테스트에 사용할 경로를 만든다.
    async def route(_row, target, _start, valid=None):
        assert valid()
        moves.append(target)

    monkeypatch.setattr(order_module, "dispatch_order_route", route)
    mode_manager.set_mode("real")
    try:
        order = order_manager.create_order(items={"A": 1, "B": 0, "C": 1, "D": 0}, total_quantity=2)
        row = order_manager.orders[order["order_id"]]
        row.update(status="IN_PROGRESS", phase="WORKING_AT_PICKUP", robot_id="robot1")
        robot.assign_order(order["order_id"], row["items"], 2, ["5", "6"], None)
        robot.set_state(RobotState.WAITING)
        order_manager.on_mqtt_result(SimpleNamespace(omx_id="omx1"), {"job_id": order["order_id"], "success": True})
        assert row["phase"] == "MOVING_TO_PICKUP"
        robot.current_node = robot.occupied_node = "6"
        robot.set_state(RobotState.WAITING)
        row["phase"] = "WORKING_AT_PICKUP"
        order_manager.on_mqtt_result(SimpleNamespace(omx_id="omx2"), {"job_id": order["order_id"], "success": True})
        assert row["phase"] == "WAITING_WORKSTATION"
        assert row["workstation_node"] is None

        # 테스트에서 경로를 선택하고 로봇을 이동시킨다.
        async def select_and_move():
            await order_manager._tick_real(row)
            await asyncio.gather(*list(order_manager._route_tasks.values()))

        asyncio.run(select_and_move())
        assert row["workstation_node"] == robot.order_workstation_node == "3"
        assert moves == ["3"]
        robot.current_node = robot.occupied_node = "3"
        robot.set_state(RobotState.WAITING)
        row["phase"] = "WORKING_AT_WORKSTATION"
        order_manager.on_mqtt_result(SimpleNamespace(omx_id="omx3"), {"job_id": order["order_id"], "success": True})
        assert row["status"] == "COMPLETED"
        assert row["phase"] == "RETURNING"
        assert robot.order_id is None
    finally:
        order_manager.returning.clear()
        mode_manager.set_mode("simulation")


# 시뮬레이션이 모의 주행으로 충전소에 복귀하는지 검증한다.
def test_simulation_uses_real_mock_navigation_and_returns_to_station():
    mode_manager.set_mode("simulation")
    mock_fms.reset()
    order_manager.orders.clear()
    order_manager.returning.clear()
    order_manager._return_started.clear()
    workstation_manager._assigned.clear()
    simulation_order_runner.tasks.clear()
    simulation_order_runner.work_seconds = 0.001

    # 주문 흐름을 재현하는 테스트 시나리오를 실행한다.
    async def scenario():
        order = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        start = monotonic()
        for step in range(2000):
            await order_manager.tick()
            mock_fms.advance_simulation(0.1, now=start + (step + 1) * 0.1)
            await asyncio.sleep(0.001)
            if order_manager.get_order(order["order_id"])["phase"] == "COMPLETED":
                break
        finished = order_manager.get_order(order["order_id"])
        assert finished["status"] == finished["phase"] == "COMPLETED"
        assert finished["workstation_node"] == "3"
        assert mock_fms.robot_snapshot("robot1", "simulation")["occupied_node"] == "0"
        await simulation_order_runner.shutdown()

    asyncio.run(scenario())


# 주문 API가 로봇과 작업대 지정 없이 품목을 접수하는지 검증한다.
def test_order_api_accepts_items_without_robot_or_workstation(monkeypatch):
    _reset(monkeypatch)
    from backend.app.routers.orders import create_order
    from backend.app.schemas.order import CreateOrderRequest

    payload = CreateOrderRequest.model_validate(
        {"order_name": "ORDER-001", "items": {"A": 2, "B": 1, "C": 0, "D": 2}, "total_quantity": 5}
    )
    created = asyncio.run(create_order(payload))
    assert created["order_name"] == "ORDER-001"
    assert created["robot_id"] is None
    assert created["workstation_node"] is None
    assert created["pickup_nodes"] == ["5", "6"]
