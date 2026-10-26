"""ESP32 없이 센서 검증·재연결·기존 OMX 통신을 확인한다."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.app.services import mqtt_manager as mqtt_module


def message(station=1, status=1, *, topic=None, payload=None, retain=False, dup=False):
    return SimpleNamespace(
        topic=topic or f"dock/station/{station}/status",
        payload=json.dumps({"station": station, "status": status}).encode() if payload is None else payload,
        retain=retain, dup=dup,
    )


@pytest.fixture
def sensor(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(mqtt_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mqtt_module, "DOCK_DEBOUNCE_S", 0.3)
    monkeypatch.setattr(mqtt_module, "DOCK_SENSOR_MAX_AGE_S", 0)
    manager = mqtt_module.MQTTManager()
    manager._on_connect(Mock(), None, {}, 0)
    return manager, clock


@pytest.mark.parametrize("station", [1, 2, 3])
def test_contact_debounce_and_release(sensor, station):
    manager, clock = sensor
    generation = manager.dock_generation()
    manager._on_message(None, None, message(station))
    assert manager.get_dock_snapshot(station)["contact"] == "CONTACT"
    assert not manager.dock_contact_valid(station, started_at=100, generation=generation)
    clock[0] += 0.31
    assert manager.dock_contact_valid(station, started_at=100, generation=generation)
    manager._on_message(None, None, message(station, 0))
    assert manager.get_dock_snapshot(station)["contact"] == "RELEASED"
    assert not manager.dock_contact_valid(station, started_at=100, generation=generation)


@pytest.mark.parametrize("msg", [
    message(payload=b'\xff'), message(payload=b'{'), message(payload=b'[]'),
    message(payload=b'null'), message(payload=b'1'),
    message(payload=b'{"station":true,"status":1}'),
    message(payload=b'{"station":1,"status":true}'),
    message(payload=b'{"station":1.0,"status":1}'),
    message(status="1"), message(status=2), message(status=-1),
    message(topic="dock/station/2/status"), message(station=4),
    message(topic="dock/station/01/status"), message(topic="dock/station/1/wrong"),
    message(topic="dock/status"), message(topic="dock/station/1/status/extra"),
])
def test_invalid_messages_do_not_change_sensors_or_register_omx(sensor, msg):
    manager, _ = sensor
    manager._on_message(None, None, msg)
    assert manager.dock_status == {}
    assert manager.omx_devices == {}


def test_bounce_and_duplicates_do_not_reset_contact_age(sensor):
    manager, clock = sensor
    manager._on_message(None, None, message())
    clock[0] += 0.2
    manager._on_message(None, None, message(status=0))
    clock[0] += 0.05
    manager._on_message(None, None, message())
    clock[0] += 0.1
    manager._on_message(None, None, message())
    assert manager.get_dock_snapshot(1)["contact_since"] == 100.25
    assert not manager.get_dock_snapshot(1)["stable"]
    clock[0] += 0.21
    assert manager.get_dock_snapshot(1)["stable"]
    # 이미 눌린 상태의 반복 발행은 새 도킹의 증거가 아니다.
    assert not manager.dock_contact_valid(1, started_at=100.4, generation=manager.dock_generation())


def test_event_mode_does_not_assume_heartbeat_but_optional_expiry_works(sensor, monkeypatch):
    manager, clock = sensor
    manager._on_message(None, None, message())
    clock[0] += 3600
    assert manager.get_dock_snapshot(1)["contact"] == "CONTACT"
    monkeypatch.setattr(mqtt_module, "DOCK_SENSOR_MAX_AGE_S", 5)
    assert manager.get_dock_snapshot(1)["contact"] == "UNKNOWN"


def test_reconnect_invalidates_old_proof_and_retained_contact(sensor):
    manager, clock = sensor
    generation = manager.dock_generation()
    manager._on_message(None, None, message())
    manager._on_disconnect(None, None, 1)
    assert manager.get_dock_snapshot(1)["contact"] == "UNKNOWN"
    manager._on_connect(Mock(), None, {}, 0)
    manager._on_message(None, None, message(retain=True))
    assert manager.get_dock_snapshot(1)["contact"] == "UNKNOWN"
    clock[0] += 1
    manager._on_message(None, None, message())
    clock[0] += 1
    assert manager.get_dock_snapshot(1)["stable"]
    manager._on_message(None, None, message(status=0))
    manager._on_message(None, None, message())
    clock[0] += 1
    assert not manager.dock_contact_valid(1, started_at=100, generation=generation)
    assert manager.dock_contact_valid(1, started_at=100, generation=manager.dock_generation())


def test_protocol_retransmission_cannot_create_new_contact(sensor):
    manager, _ = sensor
    manager._on_message(None, None, message(dup=True))
    assert manager.dock_status == {}


def test_bad_dock_message_does_not_interrupt_omx_ack_progress_result(sensor):
    manager, _ = sensor
    manager.client = Mock()
    result = Mock()
    manager.set_result_callback(result)
    def omx(kind, data):
        manager._on_message(None, None, message(topic=f"omx1/{kind}", payload=json.dumps(data).encode()))

    manager._on_message(None, None, message(payload=b'\xff'))
    omx("status", {"state": "IDLE"})
    manager.send_job("omx1", "job1", {"A": 1})
    omx("ack", {"job_id": "job1", "accepted": True})
    omx("progress", {"job_id": "job1", "current": 1, "total": 1})
    assert manager.get_omx("omx1").current_count == 1
    omx("result", {"job_id": "job1", "success": True})
    omx("result", {"job_id": "job1", "success": True})
    result.assert_called_once()
    manager.send_job("omx1", "job2", {"A": 1})
    omx("ack", {"job_id": "job2", "accepted": False})
    assert result.call_count == 2
    assert result.call_args.args[1]["success"] is False


@pytest.mark.parametrize("retained,duplicate", [(True, False), (False, True)])
def test_retained_or_duplicate_release_cannot_overwrite_live_contact(sensor, retained, duplicate):
    manager, clock = sensor
    manager._on_message(None, None, message())
    previous = manager.get_dock_snapshot(1)
    clock[0] += 1
    manager._on_message(None, None, message(status=0, retain=retained, dup=duplicate))
    current = manager.get_dock_snapshot(1)
    assert current["contact"] == "CONTACT"
    assert current["received_at"] == previous["received_at"]


def test_sensor_snapshots_are_safe_during_reconnect_and_message_writes(sensor):
    from concurrent.futures import ThreadPoolExecutor
    manager, _ = sensor
    def writer():
        for _ in range(100):
            manager._on_disconnect(None, None, 1)
            manager._on_connect(Mock(), None, {}, 0)
            manager._on_message(None, None, message())
    def reader():
        for _ in range(1000):
            sample = manager.get_dock_snapshot(1)
            if sample["trusted"]:
                assert sample["broker_connected"]
                assert sample["contact"] == "CONTACT"
            else:
                assert sample["contact"] == "UNKNOWN"
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(writer), pool.submit(reader)]
        for future in futures:
            future.result(timeout=5)
