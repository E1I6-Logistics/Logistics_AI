"""접촉 검증 토글, 도킹 액션 성공, 배차와 이탈 분리를 검증한다."""

import pytest
from action_msgs.msg import GoalStatus
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import Mock

from backend.app.models.robot import RobotState, NavigationType
from backend.app.routers import docking_settings as settings_api
from backend.app.ros2 import ros_gateway as gateway_module
from backend.app.services.docking_settings import DockingSettings
from tests.test_charging_docking import docking, begin, finish, real_executor
from tests.test_dock_mqtt import message


@pytest.fixture
def policy(monkeypatch):
    settings = DockingSettings()
    settings.set_contact_verification(True)
    monkeypatch.setattr(settings_api, "docking_settings", settings)
    monkeypatch.setattr(gateway_module, "docking_settings", settings)
    return settings


@pytest.fixture
def client(policy):
    app = FastAPI()
    app.include_router(settings_api.router)
    with TestClient(app) as client:
        yield client


def set_enabled(client, enabled):
    response = client.put("/api/docking/settings", json={"contact_verification_enabled": enabled})
    assert response.status_code == 200


def test_contact_policy_is_shared_and_validated(client):
    set_enabled(client, False)
    with TestClient(client.app) as another_screen:
        assert another_screen.get("/api/docking/settings").json() == {
            "mode": "real", "contact_verification_enabled": False,
        }
    assert client.put("/api/docking/settings", json={"contact_verification_enabled": "false"}).status_code == 422
    assert client.get("/api/docking/settings").json()["contact_verification_enabled"] is False


@pytest.mark.parametrize("sensor", ["missing", "off", "on", "disconnected", "reconnected"])
def test_contact_off_allows_successful_docking_without_sensor(client, docking, sensor):
    set_enabled(client, False)
    if sensor == "disconnected":
        docking.mqtt._on_disconnect(None, None, 1)
    begin(docking)
    if sensor in ("off", "on"):
        docking.mqtt._on_message(None, None, message(status=int(sensor == "on")))
    elif sensor == "reconnected":
        docking.mqtt._on_disconnect(None, None, 1)
        docking.mqtt._on_connect(Mock(), None, {}, 0)
    docking.gateway.on_precision_dock_result("robot1", GoalStatus.STATUS_SUCCEEDED)
    robot = docking.fleet.get_robot("robot1")
    assert robot.state == RobotState.CHARGING
    assert docking.gateway._charging_docks["robot1"]["action_confirmed"] is True
    assert docking.gateway.charging_dispatch_ready("robot1")
    assert docking.gateway.docking_station_snapshots()[0]["docking_complete"] is True
    docking.clock[0] += 11
    docking.gateway._tick_charging_docks()
    assert robot.state == RobotState.CHARGING


def test_contact_on_waits_for_both_then_off_releases_only_contact_wait(client, docking):
    begin(docking)
    docking.gateway.on_precision_dock_result("robot1", GoalStatus.STATUS_SUCCEEDED)
    assert docking.fleet.get_robot("robot1").state == RobotState.DOCKING
    set_enabled(client, False)
    docking.gateway._tick_charging_docks()
    assert docking.fleet.get_robot("robot1").state == RobotState.CHARGING


@pytest.mark.parametrize("enabled", [True, False])
def test_contact_setting_never_substitutes_for_action_success(client, docking, enabled):
    set_enabled(client, enabled)
    begin(docking)
    docking.mqtt._on_message(None, None, message())
    docking.clock[0] += .31
    docking.gateway._tick_charging_docks()
    assert docking.fleet.get_robot("robot1").state == RobotState.DOCKING
    docking.gateway.on_precision_dock_result("robot1", GoalStatus.STATUS_ABORTED)
    assert docking.fleet.get_robot("robot1").pause_reason_code == "precision_dock_failed"
    set_enabled(client, False)
    docking.gateway._tick_charging_docks()
    assert docking.fleet.get_robot("robot1").state == RobotState.PAUSED


def test_contact_off_still_enforces_docking_action_timeout(client, docking):
    set_enabled(client, False)
    begin(docking)
    docking.clock[0] += 100
    docking.gateway._tick_charging_docks()
    assert docking.fleet.get_robot("robot1").pause_reason_code == "dock_action_timeout"


def test_charging_departure_does_not_recheck_on_even_when_contact_policy_on(client, docking):
    finish(docking)
    docking.mqtt._on_disconnect(None, None, 1)
    docking.gateway._tick_charging_docks()
    assert docking.gateway.charging_dispatch_ready("robot1")
    docking.gateway._validate_dock_departure("robot1")
    assert docking.fleet.get_robot("robot1").state == RobotState.CHARGING


def test_contact_off_preserves_raw_sensor_display_without_changing_state(client, docking):
    set_enabled(client, False)
    begin(docking)
    docking.gateway.on_precision_dock_result("robot1", GoalStatus.STATUS_SUCCEEDED)
    docking.mqtt._on_message(None, None, message(status=0))
    docking.clock[0] += .31
    docking.gateway._tick_charging_docks()
    snapshot = docking.gateway.docking_station_snapshots()[0]
    assert snapshot["contact"] == "RELEASED"
    assert snapshot["docking_complete"] is True
    assert docking.fleet.get_robot("robot1").state == RobotState.CHARGING


def test_contact_off_manual_movement_does_not_leave_robot_charging(client, docking):
    set_enabled(client, False)
    begin(docking)
    docking.gateway.on_precision_dock_result("robot1", GoalStatus.STATUS_SUCCEEDED)
    docking.gateway.cmd_vel("robot1", .05, 0.)
    docking.gateway.cmd_vel("robot1", 0., 0.)
    assert docking.fleet.get_robot("robot1").state == RobotState.IDLE
    assert not docking.gateway._charging_docks


@pytest.mark.parametrize("enabled", [True, False])
def test_sensor_on_alone_never_creates_charging(client, docking, enabled):
    set_enabled(client, enabled)
    robot = docking.fleet.get_robot("robot1")
    robot.goal_node = robot.navigation_type = None
    robot.set_state(RobotState.IDLE)
    docking.mqtt._on_message(None, None, message())
    docking.clock[0] += .31
    docking.gateway._tick_charging_docks()
    assert robot.state == RobotState.IDLE
    assert not docking.gateway._charging_docks
    assert docking.gateway.docking_station_snapshots()[0]["contact"] == "CONTACT"
    assert not docking.gateway.docking_station_snapshots()[0]["docking_complete"]


def test_charging_recovery_does_not_start_station_exit_from_cached_on(client, docking, monkeypatch):
    finish(docking)
    nav, sent, cancels, _ = real_executor(docking, monkeypatch)
    nav._cancel_goal = docking.gateway._cancel_navigation_goal
    docking.node.cancel_follow_waypoints.side_effect = lambda rid, callback: callback()
    robot = docking.fleet.get_robot("robot1")
    robot.pause("station_exit_failed")
    docking.gateway._unverified_docks.add("robot1")
    docking.gateway.navigate_to_charging_station("robot1")
    docking.clock[0] += .1
    robot.pose_received_at = docking.clock[0]
    nav.tick()
    docking.node.send_station_exit.assert_not_called()
    assert sent
    assert robot.navigation_type == NavigationType.CHARGING
    assert not docking.gateway._departing_docks
