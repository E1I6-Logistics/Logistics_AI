"""주문 실행의 순서와 작업대 선택을 검증한다."""

import asyncio
import json
import logging
import pytest
# 기존 코드. AI 경로 테스트가 워커 스레드에서 기다리도록 sleep을 추가한다.
# from time import monotonic
from time import monotonic, sleep

from backend.app.services.mock_data import mock_fms
from backend.app.services.mode_service import mode_manager
from backend.app.services.order_manager import order_manager
from backend.app.services.simulation_order_runner import simulation_order_runner
from backend.app.services.workstation_manager import workstation_manager


# 테스트 사이에 공유 상태를 초기화한다.
def _reset(monkeypatch):
    mode_manager.set_mode("simulation", "algorithm")
    # 코드 기본값과 같은 2초를 명시해 .env 설정과 무관하게 세 후보를 비교하도록 한다.
    monkeypatch.setenv("KEV_DISPATCH_ETA_TIE_SECONDS", "2")
    # 시뮬레이션 이동 속도와 같은 0.2 m/s를 명시한다. .env에는 실제 로봇용 0.06이 있다.
    monkeypatch.setenv("KEV_DISPATCH_ETA_SPEED_MPS", "0.2")
    mock_fms.reset()
    order_manager.orders.clear()
    order_manager.returning.clear()
    order_manager.recovering.clear()
    order_manager._return_started.clear()
    order_manager._route_tasks.clear()
    order_manager._kev_dispatch_selector = None
    order_manager._kev_dispatch_retry_at = 0.0
    workstation_manager._assigned.clear()
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
            "route": "algorithm",
            "dispatch": "ai" if operation == "ai" else "algorithm",
            "workstation": "algorithm",
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
        assert order["driving_mode"] == "standard"
        assert order["mode"] == environment
        assert workstation_manager.choose(order["order_id"], environment, operation) == "3"

    try:
        asyncio.run(scenario())
    finally:
        mode_manager.set_mode("simulation", "algorithm")


# AI 배차 모드에서 KEV가 선택한 후보 로봇을 주문에 할당하는지 검증한다.
def test_kev_dispatch_selects_candidate_robot(monkeypatch):
    _reset(monkeypatch)

    class FakeKevSelector:
        def select_choice(self, **kwargs):
            assert set(kwargs["candidates"]) == {"robot1", "robot2", "robot3"}
            return {"choice": "robot2"}

    async def scenario():
        mode_manager.set_mode("simulation", "ai")
        order_manager._kev_dispatch_selector = FakeKevSelector()
        order = order_manager.create_order(
            items={"A": 1, "B": 0, "C": 0, "D": 0},
            total_quantity=1,
        )
        await order_manager.tick()
        assert order_manager.get_order(order["order_id"])["robot_id"] == "robot2"
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))

    try:
        asyncio.run(scenario())
    finally:
        mode_manager.set_mode("simulation", "algorithm")


# KEV 요청/응답 JSON 디버그 로그가 opt-in으로만 출력되는지 검증한다.
@pytest.mark.parametrize("debug_enabled", [True, False])
def test_kev_dispatch_debug_log_is_opt_in(monkeypatch, caplog, debug_enabled):
    _reset(monkeypatch)
    captured = {}
    if debug_enabled:
        monkeypatch.setenv("KEV_DISPATCH_DEBUG", "1")
    else:
        monkeypatch.delenv("KEV_DISPATCH_DEBUG", raising=False)
    caplog.set_level(logging.INFO, logger="backend.app.services.order_manager")

    class FakeKevSelector:
        def select_choice(self, **kwargs):
            captured.update(kwargs)
            return {
                "choice": "robot2",
                "confidence": 0.4,
                "answer_confidence": 0.6,
                "probabilities": {"robot1": 0.2, "robot2": 0.6, "robot3": 0.2},
            }

    async def scenario():
        mode_manager.set_mode("simulation", "ai")
        order_manager._kev_dispatch_selector = FakeKevSelector()
        order = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        await order_manager.tick()
        assert order_manager.get_order(order["order_id"])["robot_id"] == "robot2"
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))
        return order

    try:
        order = asyncio.run(scenario())
    finally:
        mode_manager.set_mode("simulation", "algorithm")

    assert captured["question_id"] == f"dispatch_{order['order_id']}"
    assert "order_id" not in captured["state"]["order"]
    assert [profile["robot_id"] for profile in captured["state"]["robots"]] == [
        "robot1",
        "robot2",
        "robot3",
    ]
    assert captured["instructions"] == (
        "All listed robots have passed code safety checks and are within the configured pickup ETA near-tie window. "
        "Choose exactly one listed candidate ID. Do not re-rank candidates by pickup ETA and do not treat battery as a safety threshold. "
        "Prefer an IDLE, non-returning robot when operational state differs. Use the current battery only as secondary context; "
        "it is not an estimated post-task battery. Return only one candidate ID."
    )
    assert captured["state"]["dispatch_context"] == {
        "eta_filter_applied": True,
        "minimum_pickup_eta_seconds": pytest.approx(6.780479020017512),
        "eta_tie_window_seconds": 2.0,
        "excluded_by_eta": [],
        "post_task_battery_available": False,
        "recent_workload_available": False,
    }
    assert all("pickup_distance_m" in profile and "pickup_eta_seconds" in profile for profile in captured["state"]["robots"])

    debug_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "backend.app.services.order_manager"
        and record.getMessage().startswith("KEV_DISPATCH_DEBUG ")
    ]
    if not debug_enabled:
        assert debug_messages == []
        return

    assert len(debug_messages) == 1
    record = json.loads(debug_messages[0].removeprefix("KEV_DISPATCH_DEBUG "))
    assert record == {
        "order_id": order["order_id"],
        "question_id": captured["question_id"],
        "excluded_last_robot": None,
        "rule_baseline_choice": "robot1",
        "state": captured["state"],
        "choices": captured["candidates"],
        "instructions": captured["instructions"],
        "response": {
            "probabilities": {"robot1": 0.2, "robot2": 0.6, "robot3": 0.2},
            "confidence": 0.4,
            "answer_confidence": 0.6,
            "choice": "robot2",
        },
    }


# ETA 차이가 설정 범위를 넘으면 KEV에 보내지 않고 코드가 최단 ETA 로봇을 확정하는지 검증한다.
def test_kev_dispatch_uses_code_for_clear_eta_winner(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setenv("KEV_DISPATCH_ETA_TIE_SECONDS", "0")
    calls = []

    class FakeKevSelector:
        def select_choice(self, **kwargs):
            calls.append(kwargs)
            return {"choice": "robot1"}

    async def scenario():
        mode_manager.set_mode("simulation", "ai")
        order_manager._kev_dispatch_selector = FakeKevSelector()
        order = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        await order_manager.tick()
        assert order_manager.get_order(order["order_id"])["robot_id"] == "robot2"
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))

    try:
        asyncio.run(scenario())
    finally:
        mode_manager.set_mode("simulation", "algorithm")

    assert calls == []


# ETA 근소 후보군 안에서만 쓰는 임시 코드 기준선의 우선순위를 검증한다.
def test_dispatch_rule_baseline_prefers_idle_then_battery_then_robot_id():
    profiles = [
        {"robot_id": "robot3", "state": "IDLE", "returning": False, "battery": None},
        {"robot_id": "robot2", "state": "IDLE", "returning": False, "battery": 80},
        {"robot_id": "robot1", "state": "MOVING", "returning": False, "battery": 100},
    ]

    assert order_manager._select_robot_by_dispatch_rules(profiles) == "robot2"

    profiles[0]["battery"] = 80
    profiles[0]["robot_id"] = "robot1"
    assert order_manager._select_robot_by_dispatch_rules(profiles) == "robot1"


# 배터리가 NaN·비수치·무한대여도 예외 없이 유효한 배터리보다 뒤로 가는지 검증한다.
def test_dispatch_rule_baseline_puts_invalid_battery_last():
    profiles = [
        {"robot_id": "robot1", "state": "IDLE", "returning": False, "battery": float("nan")},
        {"robot_id": "robot2", "state": "IDLE", "returning": False, "battery": "abc"},
        {"robot_id": "robot3", "state": "IDLE", "returning": False, "battery": float("inf")},
        {"robot_id": "robot4", "state": "IDLE", "returning": False, "battery": 10},
    ]

    assert order_manager._select_robot_by_dispatch_rules(profiles) == "robot4"

    # 유효한 배터리가 없으면 robot_id 오름차순으로 확정한다.
    assert order_manager._select_robot_by_dispatch_rules(profiles[:3]) == "robot1"


# 복귀 중인 로봇은 배터리가 높아도 IDLE·비복귀 로봇보다 뒤로 가는지 검증한다.
def test_dispatch_rule_baseline_prefers_non_returning_over_battery():
    profiles = [
        {"robot_id": "robot1", "state": "IDLE", "returning": True, "battery": 100},
        {"robot_id": "robot2", "state": "MOVING", "returning": True, "battery": 90},
        {"robot_id": "robot3", "state": "IDLE", "returning": False, "battery": 20},
    ]

    assert order_manager._select_robot_by_dispatch_rules(profiles) == "robot3"

    # IDLE·비복귀 후보가 없으면 복귀 중 후보끼리 배터리로 비교한다.
    assert order_manager._select_robot_by_dispatch_rules(profiles[:2]) == "robot1"


# 배차 전압 하한 미만이나 전압을 모르는 로봇을 막고, 0이면 검사하지 않는지 검증한다.
def test_battery_block_reason_uses_voltage_threshold(monkeypatch):
    from backend.app.services import order_dispatcher

    monkeypatch.setattr(order_dispatcher, "MIN_DISPATCH_BATTERY_VOLTAGE", 11.2)
    assert order_dispatcher.battery_block_reason(12.0) is None
    assert order_dispatcher.battery_block_reason(11.2) is None
    assert order_dispatcher.battery_block_reason(11.19) == "low_battery"
    assert order_dispatcher.battery_block_reason(None) == "battery_unknown"
    assert order_dispatcher.battery_block_reason(float("nan")) == "battery_unknown"
    assert order_dispatcher.battery_block_reason("abc") == "battery_unknown"

    monkeypatch.setattr(order_dispatcher, "MIN_DISPATCH_BATTERY_VOLTAGE", 0.0)
    assert order_dispatcher.battery_block_reason(None) is None
    assert order_dispatcher.battery_block_reason(9.0) is None


# 실제 로봇은 최근 판단 시간 안의 최저 전압을 쓰고, NaN 전압은 기록하지 않는지 검증한다.
def test_robot_min_battery_voltage_uses_recent_window(monkeypatch):
    from backend.app.models import robot as robot_model

    monkeypatch.setattr(robot_model, "BATTERY_VOLTAGE_WINDOW_S", 60.0)
    robot = robot_model.Robot("robot1")
    assert robot.min_battery_voltage(now=0.0) is None

    robot.update_battery(percentage=0.8, voltage=11.0, now=0.0)
    robot.update_battery(percentage=0.8, voltage=11.8, now=30.0)
    robot.update_battery(percentage=0.8, voltage=float("nan"), now=40.0)
    assert robot.battery_voltage == 11.8
    # 주행 중 강하한 11.0V가 판단 시간 안에 있으면 정지 후 전압이 올라도 최저값을 쓴다.
    assert robot.min_battery_voltage(now=50.0) == 11.0
    # 판단 시간이 지나면 오래된 기록은 빠진다.
    assert robot.min_battery_voltage(now=70.0) == 11.8
    assert robot.min_battery_voltage(now=100.0) is None
    # 전압이 없는 기존 호출도 그대로 동작한다.
    robot.update_battery(percentage=0.5)
    assert robot.battery == 0.5


# 연결이 끊기면(배터리 교체) 이전 배터리의 낮은 전압 기록을 지우고, 재연결 후 새 전압으로 판단하는지 검증한다.
def test_robot_disconnect_clears_battery_voltage_history(monkeypatch):
    from backend.app.models import robot as robot_model

    monkeypatch.setattr(robot_model, "BATTERY_VOLTAGE_WINDOW_S", 60.0)
    robot = robot_model.Robot("robot1")
    robot.set_connected(True)
    robot.update_battery(percentage=0.2, voltage=10.9, now=0.0)
    assert robot.min_battery_voltage(now=10.0) == 10.9

    robot.set_connected(False)
    assert robot.battery_voltage is None
    assert robot.min_battery_voltage(now=11.0) is None

    robot.set_connected(True)
    robot.update_battery(percentage=0.95, voltage=12.4, now=20.0)
    # 판단 시간 60초 안이어도 교체 전 10.9V 기록은 남지 않는다.
    assert robot.min_battery_voltage(now=21.0) == 12.4

    # 이미 연결된 상태에서 다시 연결 처리돼도 기록은 유지된다.
    robot.set_connected(True)
    assert robot.min_battery_voltage(now=22.0) == 12.4


# 시뮬레이션에서 전압 하한 미만 로봇은 배차 후보에서 빠지고 차단 이유가 표시되는지 검증한다.
def test_low_battery_voltage_robot_is_not_dispatched(monkeypatch):
    from backend.app.services import order_dispatcher

    _reset(monkeypatch)
    monkeypatch.setattr(order_dispatcher, "MIN_DISPATCH_BATTERY_VOLTAGE", 11.2)
    monkeypatch.setenv("SIM_BATTERY_VOLTAGE_OVERRIDE", "robot1=11.0")
    mock_fms.reset()

    assert order_dispatcher.available_robots("simulation", set()) == ["robot2", "robot3"]
    status = order_manager.dispatch_status("simulation")
    assert status["robot1"]["dispatch_available"] is False
    assert status["robot1"]["dispatch_block_reason"] == "low_battery"

    async def scenario():
        order = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        await order_manager.tick()
        assert order_manager.get_order(order["order_id"])["robot_id"] == "robot2"
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))

    asyncio.run(scenario())


# KEV 요청 오류도 디버그 모드에서 요청과 오류 정보를 한 줄로 기록하는지 검증한다.
def test_kev_dispatch_debug_log_records_request_error(monkeypatch, caplog):
    _reset(monkeypatch)
    monkeypatch.setenv("KEV_DISPATCH_DEBUG", "1")
    caplog.set_level(logging.INFO, logger="backend.app.services.order_manager")

    class FailingKevSelector:
        def select_choice(self, **kwargs):
            raise TimeoutError("simulated timeout")

    async def scenario():
        mode_manager.set_mode("simulation", "ai")
        order_manager._kev_dispatch_selector = FailingKevSelector()
        order = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        await order_manager.tick()
        assert order_manager.get_order(order["order_id"])["robot_id"] == "robot1"
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))
        return order

    try:
        order = asyncio.run(scenario())
    finally:
        mode_manager.set_mode("simulation", "algorithm")

    debug_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "backend.app.services.order_manager"
        and record.getMessage().startswith("KEV_DISPATCH_DEBUG ")
    ]
    assert len(debug_messages) == 1
    record = json.loads(debug_messages[0].removeprefix("KEV_DISPATCH_DEBUG "))
    assert record["order_id"] == order["order_id"]
    assert record["question_id"] == f"dispatch_{order['order_id']}"
    assert record["state"]["order"]["items"] == {"A": 1, "B": 0, "C": 0, "D": 0}
    assert list(record["choices"]) == ["robot1", "robot2", "robot3"]
    assert record["response"] is None
    assert record["error"] == {"type": "TimeoutError", "message": "simulated timeout"}


# KEV 호출이 실패하면 기존 규칙 기반 배차로 자동 복구하는지 검증한다.
def test_kev_dispatch_falls_back_to_algorithm(monkeypatch):
    _reset(monkeypatch)

    class FailingKevSelector:
        def select_choice(self, **kwargs):
            raise RuntimeError("KEV server unavailable")

    async def scenario():
        mode_manager.set_mode("simulation", "ai")
        order_manager._kev_dispatch_selector = FailingKevSelector()
        order = order_manager.create_order(
            items={"A": 1, "B": 0, "C": 0, "D": 0},
            total_quantity=1,
        )
        await order_manager.tick()
        assert order_manager.get_order(order["order_id"])["robot_id"] == "robot1"
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))

    try:
        asyncio.run(scenario())
    finally:
        mode_manager.set_mode("simulation", "algorithm")


# KEV 오류 fallback이 기존 목록 순서가 아니라 임시 기준선 선택값을 쓰는지 검증한다.
def test_kev_dispatch_fallback_uses_rule_baseline(monkeypatch):
    _reset(monkeypatch)
    # robot1 배터리를 낮춰 기존 목록 순서(robot1)와 기준선(배터리 최고 robot2)이 갈리게 한다.
    monkeypatch.setenv("SIM_BATTERY_OVERRIDE", "robot1=40")
    mock_fms.reset()

    class FailingKevSelector:
        def select_choice(self, **kwargs):
            raise RuntimeError("KEV server unavailable")

    async def scenario():
        mode_manager.set_mode("simulation", "ai")
        order_manager._kev_dispatch_selector = FailingKevSelector()
        order = order_manager.create_order(
            items={"A": 1, "B": 0, "C": 0, "D": 0},
            total_quantity=1,
        )
        await order_manager.tick()
        assert order_manager.get_order(order["order_id"])["robot_id"] == "robot2"
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))

    try:
        asyncio.run(scenario())
    finally:
        mode_manager.set_mode("simulation", "algorithm")


# KEV 오류 또는 cooldown fallback도 ETA 후보군 밖의 로봇을 선택하지 않는지 검증한다.
def test_kev_dispatch_fallback_stays_within_eta_tie_window(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setenv("KEV_DISPATCH_ETA_TIE_SECONDS", "1")

    class FailingKevSelector:
        def select_choice(self, **kwargs):
            assert set(kwargs["candidates"]) == {"robot2", "robot3"}
            raise RuntimeError("KEV server unavailable")

    async def scenario():
        mode_manager.set_mode("simulation", "ai")
        order_manager._kev_dispatch_selector = FailingKevSelector()
        order = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        await order_manager.tick()
        assert order_manager.get_order(order["order_id"])["robot_id"] == "robot2"
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))

    try:
        asyncio.run(scenario())
    finally:
        mode_manager.set_mode("simulation", "algorithm")


# 재시도 주문은 다른 후보가 있으면 직전 로봇을 KEV 후보에서 빼는지 검증한다.
def test_kev_dispatch_excludes_last_robot_when_others_available(monkeypatch):
    _reset(monkeypatch)
    seen = []

    class FakeKevSelector:
        def select_choice(self, **kwargs):
            seen.append(set(kwargs["candidates"]))
            return {"choice": sorted(kwargs["candidates"])[0]}

    order_manager._kev_dispatch_selector = FakeKevSelector()
    row = {
        "order_id": "retry-1",
        "items": {"A": 1, "B": 0, "C": 0, "D": 0},
        "total_quantity": 1,
        "pickup_nodes": ["5"],
        "last_robot_id": "robot1",
    }
    chosen = order_manager._select_robot_with_kev(row, ["robot1", "robot2", "robot3"], "simulation")
    assert seen == [{"robot2", "robot3"}]
    assert chosen == "robot2"

    # 직전 로봇만 남으면 그대로 배정한다.
    assert order_manager._select_robot_with_kev(row, ["robot1"], "simulation") == "robot1"
    assert len(seen) == 1


# 직전 로봇을 KEV 후보에서 뺐다면 디버그 JSON에 excluded_last_robot으로 남기는지 검증한다.
def test_kev_dispatch_debug_log_records_excluded_last_robot(monkeypatch, caplog):
    _reset(monkeypatch)
    monkeypatch.setenv("KEV_DISPATCH_DEBUG", "1")
    caplog.set_level(logging.INFO, logger="backend.app.services.order_manager")

    class FakeKevSelector:
        def select_choice(self, **kwargs):
            return {"choice": "robot3", "probabilities": {"robot2": 0.4, "robot3": 0.6}}

    order_manager._kev_dispatch_selector = FakeKevSelector()
    row = {
        "order_id": "retry-1",
        "items": {"A": 1, "B": 0, "C": 0, "D": 0},
        "total_quantity": 1,
        "pickup_nodes": ["5"],
        "last_robot_id": "robot1",
    }
    assert order_manager._select_robot_with_kev(row, ["robot1", "robot2", "robot3"], "simulation") == "robot3"

    debug_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "backend.app.services.order_manager"
        and record.getMessage().startswith("KEV_DISPATCH_DEBUG ")
    ]
    assert len(debug_messages) == 1
    record = json.loads(debug_messages[0].removeprefix("KEV_DISPATCH_DEBUG "))
    assert record["excluded_last_robot"] == "robot1"
    assert list(record["choices"]) == ["robot2", "robot3"]
    assert record["state"]["order"]["last_robot_id"] == "robot1"

    # 제외 뒤 후보가 한 대만 남으면 KEV를 부르지 않고 skip 로그에 제외 로봇을 남긴다.
    caplog.clear()
    assert order_manager._select_robot_with_kev(row, ["robot1", "robot2"], "simulation") == "robot2"
    messages = [record.getMessage() for record in caplog.records]
    assert not any(message.startswith("KEV_DISPATCH_DEBUG ") for message in messages)
    assert any("skipped (single candidate)" in m and "excluded_last_robot=robot1" in m for m in messages)


# KEV 실패 후 쿨다운 동안에는 KEV를 다시 호출하지 않고 기존 배차를 사용하는지 검증한다.
def test_kev_dispatch_failure_starts_cooldown(monkeypatch):
    _reset(monkeypatch)
    calls = []

    class FailingKevSelector:
        def select_choice(self, **kwargs):
            calls.append(kwargs["question_id"])
            raise RuntimeError("KEV server unavailable")

    async def scenario():
        mode_manager.set_mode("simulation", "ai")
        order_manager._kev_dispatch_selector = FailingKevSelector()
        first = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        second = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        await order_manager.tick()
        assert len(calls) == 1
        assert order_manager.get_order(first["order_id"])["robot_id"] == "robot1"
        assert order_manager.get_order(second["order_id"])["robot_id"] == "robot2"
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))

    try:
        asyncio.run(scenario())
    finally:
        mode_manager.set_mode("simulation", "algorithm")


# KEV 응답을 기다리는 동안 주문 lock이 풀려 있는지 검증한다.
def test_kev_dispatch_call_runs_outside_order_lock(monkeypatch):
    _reset(monkeypatch)
    lock_free = []

    class FakeKevSelector:
        def select_choice(self, **kwargs):
            # 다른 스레드에서 lock을 바로 잡을 수 있어야 한다.
            acquired = order_manager.lock.acquire(blocking=False)
            if acquired:
                order_manager.lock.release()
            lock_free.append(acquired)
            return {"choice": "robot2"}

    async def scenario():
        mode_manager.set_mode("simulation", "ai")
        order_manager._kev_dispatch_selector = FakeKevSelector()
        order = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        await order_manager.tick()
        assert lock_free == [True]
        assert order_manager.get_order(order["order_id"])["robot_id"] == "robot2"
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))

    try:
        asyncio.run(scenario())
    finally:
        mode_manager.set_mode("simulation", "algorithm")


# KEV 응답을 기다리는 동안 후보가 바뀌면 그 결과를 버리고 바뀐 후보로 다시 묻는지 검증한다.
def test_kev_dispatch_discards_result_when_candidates_change(monkeypatch):
    _reset(monkeypatch)
    seen = []

    class FakeKevSelector:
        def select_choice(self, **kwargs):
            seen.append(sorted(kwargs["candidates"]))
            if len(seen) == 1:
                # 첫 응답 전에 robot2가 배차 불가 상태가 된다.
                with mock_fms._lock:
                    mock_fms._robots["robot2"]["status"] = "PAUSED"
                return {"choice": "robot2"}
            return {"choice": "robot3"}

    async def scenario():
        mode_manager.set_mode("simulation", "ai")
        order_manager._kev_dispatch_selector = FakeKevSelector()
        order = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        await order_manager.tick()
        assert seen == [["robot1", "robot2", "robot3"], ["robot1", "robot3"]]
        assert order_manager.get_order(order["order_id"])["robot_id"] == "robot3"
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))

    try:
        asyncio.run(scenario())
    finally:
        mode_manager.set_mode("simulation", "algorithm")


# 주문 API로 등록한 AI 모드 주문도 KEV 배차를 사용하는지 검증한다.
def test_order_api_ai_mode_uses_kev_dispatch(monkeypatch):
    _reset(monkeypatch)
    from backend.app.routers.orders import create_order
    from backend.app.schemas.order import CreateOrderRequest

    class FakeKevSelector:
        def select_choice(self, **kwargs):
            return {"choice": "robot3"}

    async def scenario():
        mode_manager.set_mode("simulation", "ai")
        order_manager._kev_dispatch_selector = FakeKevSelector()
        order = await create_order(
            CreateOrderRequest(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        )
        assert order_manager.get_order(order["order_id"])["operation_mode"] == "ai"
        await order_manager.tick()
        assert order_manager.get_order(order["order_id"])["robot_id"] == "robot3"
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))

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
    robot.set_connected(True)
    robot.update_pose(0, 0, 0)
    # 배차 전압 하한 검사를 통과하도록 정상 전압을 받은 상태로 둔다.
    robot.update_battery(percentage=0.9, voltage=12.0)
    robot.occupied_node = "0"
    robot.set_state(RobotState.IDLE)
    monkeypatch.setattr(fleet_manager, "get_all_robots", lambda: [robot])
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
    # 기존 코드. order_route_dispatcher는 테스트에서 더 이상 쓰지 않는다.
    # from backend.app.services import route_model_catalog, order_route_dispatcher
    from backend.app.services import route_model_catalog, llm_route_preview, mode_service

    monkeypatch.setattr(route_model_catalog, "require_enabled_model", lambda *_: None)
    # 운영 AI 모드는 KEV 배차만 켜고 경로는 algorithm이다. 이 테스트는 AI 경로만 따로 검증하므로
    # 경로만 ai로 켜고 배차는 algorithm으로 둬 실제 KEV 서버를 호출하지 않게 한다.
    monkeypatch.setitem(
        mode_service.FEATURE_ENGINES, "ai", {"route": "ai", "dispatch": "algorithm", "workstation": "algorithm"}
    )

    # 기존 코드. 전역 asyncio.to_thread를 바꿔 KEV 배차의 to_thread까지 가로챘다.
    # async def preview(_function, _graph, start, target, **_options):
    #     snapshot = mock_fms.robot_snapshot("robot1", "simulation")
    #     assert snapshot["occupied_node"] == str(start)
    #     assert snapshot["stop_requested"] is True
    #     assert snapshot["route"] is None
    #     await asyncio.sleep(0.01)
    #     return {"path": [str(start), str(target)]}
    #
    # monkeypatch.setattr(order_route_dispatcher.asyncio, "to_thread", preview)
    # 테스트에서 경로 선택 결과를 미리 계산한다. 실제 asyncio.to_thread가 워커 스레드에서 호출한다.
    def preview(_graph, start, target, **_options):
        snapshot = mock_fms.robot_snapshot("robot1", "simulation")
        assert snapshot["occupied_node"] == str(start)
        assert snapshot["stop_requested"] is True
        assert snapshot["route"] is None
        sleep(0.01)
        return {"path": [str(start), str(target)]}

    monkeypatch.setattr(llm_route_preview, "preview_llm_route", preview)
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
    # 기존 코드. order_route_dispatcher는 테스트에서 더 이상 쓰지 않는다.
    # from backend.app.services import route_model_catalog, order_route_dispatcher
    from backend.app.services import route_model_catalog, llm_route_preview, mode_service

    monkeypatch.setattr(route_model_catalog, "require_enabled_model", lambda *_: None)
    # 운영 AI 모드는 KEV 배차만 켜고 경로는 algorithm이다. 이 테스트는 AI 경로만 따로 검증하므로
    # 경로만 ai로 켜고 배차는 algorithm으로 둬 실제 KEV 서버를 호출하지 않게 한다.
    monkeypatch.setitem(
        mode_service.FEATURE_ENGINES, "ai", {"route": "ai", "dispatch": "algorithm", "workstation": "algorithm"}
    )

    # 기존 코드. 전역 asyncio.to_thread를 바꿔 KEV 배차의 to_thread까지 가로챘다.
    # async def preview(_function, _graph, start, target, **_options):
    #     if str(target) == "3":
    #         raise ValueError("순환 방지 조건을 만족하는 이동 후보가 없습니다: current=10")
    #     return {"path": [str(start), str(target)]}
    #
    # monkeypatch.setattr(order_route_dispatcher.asyncio, "to_thread", preview)
    # 테스트에서 경로 선택 결과를 미리 계산한다. 실제 asyncio.to_thread가 워커 스레드에서 호출한다.
    def preview(_graph, start, target, **_options):
        if str(target) == "3":
            raise ValueError("순환 방지 조건을 만족하는 이동 후보가 없습니다: current=10")
        return {"path": [str(start), str(target)]}

    monkeypatch.setattr(llm_route_preview, "preview_llm_route", preview)

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


# 시뮬레이션 /api/robots가 주문 중인 로봇을 IDLE이어도 배차 불가로 표시하는지 검증한다.
def test_robots_api_reports_dispatch_status_in_simulation(monkeypatch):
    _reset(monkeypatch)
    from backend.app.routers.robots import fetch_robots

    async def scenario():
        order = order_manager.create_order(items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1)
        before = {row["robot_id"]: row for row in await fetch_robots()}
        assert all(row["dispatch_available"] for row in before.values())
        assert all(row["dispatch_block_reason"] is None for row in before.values())
        assert all(row["assigned_order_id"] is None for row in before.values())

        await order_manager.tick()
        # 픽업대에서 기다리는 것처럼 주행 상태는 IDLE이지만 주문은 진행 중인 상태를 만든다.
        mock_fms.stop_robot("robot1")
        after = {row["robot_id"]: row for row in await fetch_robots()}
        assert after["robot1"]["status"] == "IDLE"
        assert after["robot1"]["assigned_order_id"] == order["order_id"]
        assert after["robot1"]["dispatch_available"] is False
        assert after["robot1"]["dispatch_block_reason"] == "assigned_order"
        assert after["robot2"]["dispatch_available"] is True
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))

    asyncio.run(scenario())


# 실제 모드 /api/robots가 order_dispatcher와 같은 기준으로 막힌 이유를 표시하는지 검증한다.
def test_robots_api_reports_dispatch_status_in_real_mode(monkeypatch):
    _reset(monkeypatch)
    from backend.app.models.robot import Robot, RobotState
    from backend.app.ros2.ros_gateway import ros_gateway
    from backend.app.routers.robots import fetch_robots
    from backend.app.services.fleet_manager import fleet_manager

    robots = []
    for robot_id, node in (("robot1", "0"), ("robot2", "1"), ("robot3", "2")):
        robot = Robot(robot_id)
        robot.set_connected(True)
        robot.update_pose(0, 0, 0)
        # 배차 전압 하한 검사를 통과하도록 정상 전압을 받은 상태로 둔다.
        robot.update_battery(percentage=0.9, voltage=12.0)
        robot.occupied_node = node
        robot.set_state(RobotState.IDLE)
        robots.append(robot)
    robots[2].set_connected(False)
    monkeypatch.setattr(fleet_manager, "get_all_robots", lambda: robots)
    monkeypatch.setattr(ros_gateway, "navigation_active", lambda robot_id: robot_id == "robot2")
    monkeypatch.setattr(type(mode_manager), "mode", property(lambda _self: "real"))
    order_manager.orders["o1"] = {"order_id": "o1", "robot_id": "robot1", "status": "IN_PROGRESS", "mode": "real"}

    rows = {row["robot_id"]: row for row in asyncio.run(fetch_robots())}
    assert rows["robot1"]["assigned_order_id"] == "o1"
    assert rows["robot1"]["dispatch_block_reason"] == "assigned_order"
    assert rows["robot2"]["dispatch_block_reason"] == "navigating"
    assert rows["robot3"]["dispatch_block_reason"] == "disconnected"
    assert not any(row["dispatch_available"] for row in rows.values())


# 실제 모드에서 전압이 낮거나 아직 전압을 받지 못한 로봇을 배차하지 않고 이유를 표시하는지 검증한다.
def test_real_robot_battery_voltage_blocks_dispatch(monkeypatch):
    _reset(monkeypatch)
    from backend.app.models.robot import Robot, RobotState
    from backend.app.ros2.ros_gateway import ros_gateway
    from backend.app.routers.robots import fetch_robots
    from backend.app.services import order_dispatcher
    from backend.app.services.fleet_manager import fleet_manager

    monkeypatch.setattr(order_dispatcher, "MIN_DISPATCH_BATTERY_VOLTAGE", 11.2)
    robots = []
    for robot_id, node, voltage in (("robot1", "0", 12.0), ("robot2", "1", 11.0), ("robot3", "2", None)):
        robot = Robot(robot_id)
        robot.set_connected(True)
        robot.update_pose(0, 0, 0)
        robot.update_battery(percentage=0.5, voltage=voltage)
        robot.occupied_node = node
        robot.set_state(RobotState.IDLE)
        robots.append(robot)
    monkeypatch.setattr(fleet_manager, "get_all_robots", lambda: robots)
    monkeypatch.setattr(ros_gateway, "navigation_active", lambda _id: False)
    monkeypatch.setattr(type(mode_manager), "mode", property(lambda _self: "real"))

    assert order_dispatcher.available_robots("real", set()) == ["robot1"]
    rows = {row["robot_id"]: row for row in asyncio.run(fetch_robots())}
    assert rows["robot1"]["dispatch_available"] is True
    assert rows["robot2"]["dispatch_block_reason"] == "low_battery"
    assert rows["robot2"]["battery_voltage_min"] == 11.0
    assert rows["robot3"]["dispatch_block_reason"] == "battery_unknown"
    assert rows["robot3"]["battery_voltage"] is None


# 다른 스레드가 주문 lock을 잡고 있으면 기다리지 않고 status_busy로 응답하는지 검증한다.
def test_robots_api_does_not_wait_for_order_lock(monkeypatch):
    import threading

    _reset(monkeypatch)
    from backend.app.routers.robots import fetch_robots

    locked = threading.Event()
    release = threading.Event()

    def hold_lock():
        with order_manager.lock:
            locked.set()
            release.wait(5)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    try:
        locked.wait(5)
        started = monotonic()
        rows = asyncio.run(fetch_robots())
        assert monotonic() - started < 0.5
        assert all(row["dispatch_available"] is None for row in rows)
        assert all(row["dispatch_block_reason"] == "status_busy" for row in rows)
    finally:
        release.set()
        thread.join()
