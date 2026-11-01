"""실제 브로커·Pi 없이 팔레트 관측, 기존 작업 흐름, 재시작 예외를 검증한다."""
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.app.models.omx import OMX
from backend.app.routers import omx as api
from backend.app.services import mqtt_manager as mqtt_module
from backend.app.services import omx_stock as stock_module
from backend.app.services import omx_maintenance as maintenance


def msg(kind, data, robot="omx1", retain=False):
    return SimpleNamespace(topic=f"{robot}/{kind}", payload=json.dumps(data).encode(), retain=retain)


def payload(robot="omx1", **changes):
    items = stock_module.PALLETS[robot]
    return dict(schema_version=1, robot_id=robot, at=100., observed_at=98., valid_for_s=12.5,
                stock={k: "PRESENT" for k in items}, items={k: {"reason": "물체 크기 변화"} for k in items}, **changes)


@pytest.fixture
def setup(monkeypatch):
    clock = [100.]
    monkeypatch.setattr(stock_module.time, "time", lambda: clock[0])
    monkeypatch.setattr(stock_module.time, "monotonic", lambda: clock[0])
    manager = mqtt_module.MQTTManager()
    manager.client = Mock()
    manager._on_message(None, None, msg("status", {"state": "IDLE"}))
    monkeypatch.setattr(api, "mqtt_manager", manager)
    monkeypatch.setattr(api, "world_to_pixel", lambda x, y: (x * 10, y * 10))
    monkeypatch.setattr(api, "mode_manager", SimpleNamespace(mode="real"))
    monkeypatch.delenv("FMS_OMX_SSH_TARGETS", raising=False)
    return manager, clock


def test_subscribe_and_snapshot_mapping(setup):
    manager, _ = setup
    broker = Mock()
    manager._on_connect(broker, None, {}, 0)
    assert any(call.args == ("+/stock",) for call in broker.subscribe.call_args_list)
    for robot in ("omx1", "omx2"):
        manager._on_message(None, None, msg("status", {"state": "IDLE"}, robot))
        manager._on_message(None, None, msg("stock", payload(robot), robot))
        row = manager.get_omx(robot).snapshot()
        assert set(row["stock"]["palettes"]) == set(stock_module.PALLETS[robot])
        assert all(p["state"] == "PRESENT" for p in row["stock"]["palettes"].values())
        assert row["stock"]["exact_item_count"] is None
    manager.client.publish.assert_not_called()


def test_http_all_four_even_before_discovery(setup):
    app = FastAPI(); app.include_router(api.router)
    with TestClient(app) as client:
        response = client.get("/api/omx")
        assert response.status_code == 200
        rows = response.json()
    assert len(rows) == 4
    assert rows[0]["connected"]
    assert not rows[1]["connected"]
    assert rows[1]["stock"]["palettes"]["C"]["state"] == "UNKNOWN"
    assert rows[2]["stock"]["palettes"] == {} and not rows[2]["stock"]["supported"]
    assert len(setup[0].omx_devices) == 1


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"schema_version": 2}, {"robot_id": "omx2"},
    {"stock": {"A": "PRESENT"}}, {"stock": {"A": 1, "B": "EMPTY"}},
    {"stock": {"A": [], "B": "EMPTY"}}, {"stock": {"A": "YES", "B": "EMPTY"}},
    {"valid_for_s": 0}, {"valid_for_s": 100000}, {"valid_for_s": float("nan")},
    {"at": 200}, {"at": 50}, {"at": float("inf")}, {"observed_at": 101},
    {"observed_at": 80}, {"observed_at": None},
])
def test_invalid_stock_never_creates_presence(setup, change):
    manager, _ = setup
    data = payload(); data.update(change)
    manager._on_message(None, None, msg("stock", data))
    assert not manager.get_omx("omx1").snapshot()["stock"]["fresh"]


def test_received_heartbeat_does_not_extend_observation_lifetime(setup):
    manager, clock = setup
    data = payload()
    for now in (100, 104, 108, 110):
        clock[0] = now
        manager._on_message(None, None, msg("status", {"state": "IDLE"}))
        data["at"] = now
        manager._on_message(None, None, msg("stock", data))
        assert manager.get_omx("omx1").snapshot()["stock"]["fresh"]
    clock[0] = 111
    manager._on_message(None, None, msg("status", {"state": "IDLE"}))
    assert not manager.get_omx("omx1").snapshot()["stock"]["fresh"]


def test_disconnect_reconnect_and_retained_stock_cannot_restore_old_result(setup):
    manager, _ = setup
    manager._on_message(None, None, msg("stock", payload()))
    manager._on_disconnect(None, None, 1)
    assert not manager.get_omx("omx1").snapshot()["connected"]
    manager._on_message(None, None, msg("status", {"state": "IDLE"}))
    manager._on_message(None, None, msg("stock", payload(), retain=True))
    assert not manager.get_omx("omx1").snapshot()["stock"]["fresh"]
    manager._on_message(None, None, msg("stock", payload()))
    assert manager.get_omx("omx1").snapshot()["stock"]["fresh"]


def test_heartbeat_gap_and_recovery_wait_invalidate_presence(setup):
    manager, clock = setup
    manager._on_message(None, None, msg("stock", payload()))
    clock[0] += 6
    manager._on_message(None, None, msg("status", {"state": "IDLE"}))
    assert not manager.get_omx("omx1").snapshot()["stock"]["fresh"]
    manager._on_message(None, None, msg("status", {"state": "ERROR", "detail": "RECOVERY_WAIT"}))
    manager._on_message(None, None, msg("stock", payload()))
    assert manager.get_omx("omx1").snapshot()["detail"] == "RECOVERY_WAIT"
    assert not manager.get_omx("omx1").snapshot()["stock"]["fresh"]


def test_existing_order_results_and_dedup_unchanged(setup):
    manager, _ = setup
    callback = Mock(); manager.set_result_callback(callback)
    manager._on_message(None, None, msg("stock", payload()))
    manager.send_job("omx1", "job1", {"A": 1})
    assert manager._pending_jobs == {("omx1", "job1")}
    assert not manager.get_omx("omx1").snapshot()["stock"]["fresh"]
    manager._on_message(None, None, msg("ack", {"job_id": "job1", "accepted": True}))
    manager._on_message(None, None, msg("progress", {"job_id": "job1", "current": 1, "total": 1, "count_kind": "motion_cycles"}))
    result = {"job_id": "job1", "success": True, "physical_unloading_verified": False}
    manager._on_message(None, None, msg("result", result))
    manager._on_message(None, None, msg("result", result))
    callback.assert_called_once()
    snapshot = manager.get_omx("omx1").snapshot()
    assert snapshot["count_kind"] == "motion_cycles"
    assert snapshot["physical_unloading_verified"] is False
    assert snapshot["current_count"] == 1


@pytest.mark.parametrize("data", [[], None, 1, "bad"])
def test_nonobject_payloads_do_not_break_callback(setup, data):
    manager, _ = setup
    manager._on_message(None, None, msg("status", data))
    manager._on_message(None, None, msg("stock", data))
    assert manager.get_omx("omx1").state == "IDLE"


def test_out_of_order_stock_and_concurrent_reads(setup):
    manager, _ = setup
    observation = manager.get_omx("omx1").pallet_observation
    observation.update(payload())
    old = payload(); old.update(observed_at=97, stock={"A": "EMPTY", "B": "EMPTY"})
    assert not observation.update(old)
    def run(i):
        if i % 2: observation.update(payload())
        return observation.snapshot(connected=True, idle=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(run, range(100)))
    assert all(r["palettes"]["A"]["state"] == "PRESENT" for r in rows)


@pytest.fixture
def configured(setup, monkeypatch):
    monkeypatch.setenv("FMS_OMX_SSH_TARGETS", '{"omx1":"omx01@192.168.0.151"}')
    return setup


def test_restart_fixed_command_and_order_interlock(configured, monkeypatch):
    manager, _ = configured
    def ssh(command, **kwargs):
        assert command[-4:] == ["systemctl", "--user", "restart", "omx-manager@omx1.service"]
        assert "BatchMode=yes" in command and "StrictHostKeyChecking=yes" in command
        assert kwargs["timeout"] == 65
        with pytest.raises(RuntimeError, match="재시작"):
            manager.send_job("omx1", "blocked", {"A": 1})
        with pytest.raises(HTTPException) as e: maintenance.restart_service(manager, "omx1")
        assert e.value.status_code == 409
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(maintenance.subprocess, "run", ssh)
    result = maintenance.restart_service(manager, "omx1")
    assert result["accepted"] and not manager.get_omx("omx1").connected
    assert manager._maintenance_devices == set() and manager._pending_jobs == set()


@pytest.mark.parametrize("error,status", [(subprocess.TimeoutExpired("ssh", 65), 504), (FileNotFoundError(), 503), (None, 502)])
def test_restart_failures_release_interlock(configured, monkeypatch, error, status):
    manager, _ = configured
    def ssh(*a, **k):
        if error: raise error
        return SimpleNamespace(returncode=255)
    monkeypatch.setattr(maintenance.subprocess, "run", ssh)
    with pytest.raises(HTTPException) as e: maintenance.restart_service(manager, "omx1")
    assert e.value.status_code == status
    assert manager._maintenance_devices == set()


def test_restart_denied_during_pending_or_working(configured, monkeypatch):
    manager, _ = configured
    ssh = Mock(); monkeypatch.setattr(maintenance.subprocess, "run", ssh)
    manager._pending_jobs.add(("omx1", "old"))
    with pytest.raises(HTTPException) as e: maintenance.restart_service(manager, "omx1")
    assert e.value.status_code == 409
    manager._pending_jobs.clear(); manager.get_omx("omx1").state = "WORKING"
    with pytest.raises(HTTPException): maintenance.restart_service(manager, "omx1")
    ssh.assert_not_called()


@pytest.mark.parametrize("target", ["bad", "root@host;reboot", "-oProxyCommand=evil", "omx@host $(evil)", 1])
def test_bad_ssh_target_never_executes(setup, monkeypatch, target):
    monkeypatch.setenv("FMS_OMX_SSH_TARGETS", json.dumps({"omx1": target}))
    assert maintenance.target_for("omx1") is None


def test_restart_http_confirmation_mode_and_opt_in(setup, monkeypatch):
    app = FastAPI(); app.include_router(api.router)
    ssh = Mock(); monkeypatch.setattr(maintenance.subprocess, "run", ssh)
    with TestClient(app) as client:
        assert client.post("/api/omx/omx8/restart", json={"confirm": True}).status_code == 404
        assert client.post("/api/omx/omx1/restart", json={"confirm": "true"}).status_code == 422
        assert client.post("/api/omx/omx1/restart", json={"confirm": False}).status_code == 400
        assert client.post("/api/omx/omx1/restart", json={"confirm": True}).status_code == 503
        monkeypatch.setattr(api, "mode_manager", SimpleNamespace(mode="simulation"))
        assert client.post("/api/omx/omx1/restart", json={"confirm": True}).status_code == 409
    ssh.assert_not_called()


def test_slow_valid_omx_observation_interval_is_supported(setup):
    manager, _ = setup
    data = payload(); data["valid_for_s"] = 750  # OMX 허용 최대 interval 300초 × 2.5
    manager._on_message(None, None, msg("stock", data))
    assert manager.get_omx("omx1").snapshot()["stock"]["fresh"]
