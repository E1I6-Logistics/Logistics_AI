"""Zenoh connection synchronization must not depend on frontend polling."""

import asyncio

import backend.app.services.connection_sync_service as connection_sync_module
from backend.app.services.connection_sync_service import ConnectionSyncService


def test_refresh_uses_worker_thread_helper_and_caches_snapshot(monkeypatch):
    worker_calls = []
    synchronized = []
    devices = [{"name": "robot1", "connected": True}]

    async def run_in_worker(callback):
        worker_calls.append(callback)
        return callback()

    def fetch_connections():
        return devices

    monkeypatch.setattr(connection_sync_module.asyncio, "to_thread", run_in_worker)
    service = ConnectionSyncService(fetch_connections, synchronized.append)

    result = asyncio.run(service.refresh())

    assert worker_calls == [fetch_connections]
    assert synchronized == [devices]
    assert result == {"devices": devices}

    result["devices"][0]["name"] = "changed"
    assert service.response() == {"devices": devices}


def test_refresh_keeps_last_successful_snapshot_when_zenoh_is_unavailable(monkeypatch):
    devices = [{"name": "robot1", "connected": True}]
    synchronized = []
    service = ConnectionSyncService(lambda: devices, synchronized.append)

    async def run_in_worker(callback):
        return callback()

    monkeypatch.setattr(connection_sync_module.asyncio, "to_thread", run_in_worker)

    assert asyncio.run(service.refresh()) == {"devices": devices}

    def unavailable():
        raise ConnectionError("Zenoh unavailable")

    service._fetch_connections = unavailable

    assert asyncio.run(service.refresh()) == {"devices": devices, "status": "unavailable"}
    assert synchronized == [devices]
