"""실제 전압 토글의 API·배차·표시 일치와 시뮬레이션 격리를 검증한다."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.models.robot import RobotState
from backend.app.routers import dispatch_settings as settings_api, robots as robots_api
from backend.app.services import order_dispatcher as dispatch, mock_data
from backend.app.services.dispatch_settings import DispatchSettings
from backend.app.services.fleet_manager import fleet_manager
from backend.app.services.mode_service import mode_manager
from tests.test_charging_docking import docking


@pytest.fixture
def policy(monkeypatch):
    settings = DispatchSettings(11.2)
    monkeypatch.setattr(settings_api, "dispatch_settings", settings)
    monkeypatch.setattr(dispatch, "dispatch_settings", settings)
    return settings


@pytest.fixture
def client(policy):
    # 실제 서버 lifespan 없이 API만 실행한다. ROS·MQTT·로봇 주행은 시작하지 않는다.
    app = FastAPI()
    app.include_router(settings_api.router)
    app.include_router(robots_api.router)
    with TestClient(app) as client:
        yield client


def configure_real_robot(ctx, monkeypatch, voltage=11.1):
    robot = ctx.fleet.get_robot("robot1")
    robot.navigation_type = robot.goal_node = robot.route = None
    robot.set_state(RobotState.IDLE)
    if voltage is None:
        robot.battery_voltage = None
        robot._battery_voltage_samples.clear()
    else:
        robot.update_battery(80, voltage=voltage)
    for rid in ("robot2", "robot3"):
        ctx.fleet.get_robot(rid).set_connected(False)
    monkeypatch.setattr(fleet_manager, "get_all_robots", ctx.fleet.get_all_robots)
    monkeypatch.setattr(fleet_manager, "get_robot", ctx.fleet.get_robot)
    monkeypatch.setattr(dispatch, "ros_gateway", ctx.gateway)
    monkeypatch.setattr(dispatch, "monotonic", lambda: ctx.clock[0])
    monkeypatch.setattr(mode_manager, "_mode", "real")
    monkeypatch.setattr(robots_api, "world_to_pixel", lambda *_: (0, 0))
    return robot


def test_settings_are_shared_across_clients_and_survive_browser_reload(client):
    assert client.get("/api/dispatch/settings").json()["battery_voltage_check_enabled"] is True
    changed = client.put("/api/dispatch/settings", json={"battery_voltage_check_enabled": False})
    assert changed.status_code == 200
    assert changed.json()["mode"] == "real"
    with TestClient(client.app) as another_screen:
        assert another_screen.get("/api/dispatch/settings").json() == changed.json()
        another_screen.put("/api/dispatch/settings", json={"battery_voltage_check_enabled": True})
    assert client.get("/api/dispatch/settings").json()["battery_voltage_check_enabled"] is True


@pytest.mark.parametrize("payload", [{}, {"battery_voltage_check_enabled": "false"},
                                    {"battery_voltage_check_enabled": None},
                                    {"battery_voltage_check_enabled": False, "mode": "simulation"}])
def test_invalid_settings_do_not_change_policy(client, payload):
    assert client.put("/api/dispatch/settings", json=payload).status_code == 422
    assert client.get("/api/dispatch/settings").json()["battery_voltage_check_enabled"] is True


@pytest.mark.parametrize("operation_mode", ["algorithm", "ai"])
@pytest.mark.parametrize("voltage", [11.1, None])
def test_api_toggle_changes_real_dispatch_and_reported_block_reason(
    client, docking, monkeypatch, operation_mode, voltage
):
    robot = configure_real_robot(docking, monkeypatch, voltage)
    monkeypatch.setattr(mode_manager, "_operation_mode", operation_mode)
    expected = "battery_unknown" if voltage is None else "low_battery"
    assert dispatch.available_robots("real", set(), operation_mode=operation_mode) == []
    assert client.get("/api/robots").json()[0]["dispatch_battery_block_reason"] == expected

    client.put("/api/dispatch/settings", json={"battery_voltage_check_enabled": False})
    assert dispatch.available_robots("real", set(), operation_mode=operation_mode) == ["robot1"]
    telemetry = client.get("/api/robots").json()[0]
    assert telemetry["dispatch_battery_block_reason"] is None
    assert telemetry["battery_voltage"] == voltage

    # OFF 중 정상 전압을 받더라도 과거 저전압 기록은 남아 있어야 한다.
    if voltage is not None:
        robot.update_battery(80, voltage=12.0)
    client.put("/api/dispatch/settings", json={"battery_voltage_check_enabled": True})
    assert dispatch.available_robots("real", set(), operation_mode=operation_mode) == []


@pytest.mark.parametrize("blocker", ["busy", "offline", "paused", "stale_pose"])
def test_voltage_off_keeps_other_real_dispatch_guards(client, docking, monkeypatch, blocker):
    robot = configure_real_robot(docking, monkeypatch)
    client.put("/api/dispatch/settings", json={"battery_voltage_check_enabled": False})
    busy = {"robot1"} if blocker == "busy" else set()
    if blocker == "offline":
        robot.set_connected(False)
    elif blocker == "paused":
        robot.pause("station_exit_failed")
    elif blocker == "stale_pose":
        robot.pose_received_at = docking.clock[0] - 3
    assert dispatch.available_robots("real", busy) == []


def test_real_toggle_does_not_change_simulation_filter_or_telemetry(client, monkeypatch):
    row = dict(robot_id="robot1", connection_state="ONLINE", status="IDLE", occupied_node="0",
               occupied_edge=None, goal_node=None, route=None, x=0., y=0., battery_voltage=11.1)
    monkeypatch.setattr(mock_data.mock_fms, "robot_snapshots", lambda _: [row])
    monkeypatch.setattr(mock_data, "world_to_pixel", lambda *_: (0, 0))
    for enabled in (True, False, True):
        client.put("/api/dispatch/settings", json={"battery_voltage_check_enabled": enabled})
        assert dispatch.available_robots("simulation", set()) == []
        assert mock_data.mock_fms._snapshot(row, "simulation")["dispatch_battery_block_reason"] == "low_battery"


def test_zero_environment_initializes_off_and_can_reenable_real_only():
    settings = DispatchSettings(0)
    assert settings.snapshot()["battery_voltage_check_enabled"] is False
    assert settings.battery_block_reason(None, mode="real") is None
    settings.set_voltage_check(True)
    assert settings.snapshot()["minimum_dispatch_voltage"] == 11.2
    assert settings.battery_block_reason(11.1, mode="real") == "low_battery"
    assert settings.battery_block_reason(None, mode="simulation") is None


def test_server_restart_uses_configured_startup_policy():
    settings = DispatchSettings(11.2)
    settings.set_voltage_check(False)
    assert DispatchSettings(11.2).snapshot()["battery_voltage_check_enabled"] is True
