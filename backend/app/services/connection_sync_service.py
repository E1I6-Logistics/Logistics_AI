# Real 모드 로봇의 Zenoh 연결 상태를 백엔드가 주기적으로 동기화한다.

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from ..ros2.ros_gateway import ros_gateway
from .zenoh_connection_manager import zenoh_connection_manager


class ConnectionSyncService:
    # 마지막으로 성공한 Zenoh 연결 목록을 API 응답용으로 보관한다.

    def __init__(
        self,
        fetch_connections: Callable[[], list[dict[str, Any]]] | None = None,
        sync_connected_robots: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> None:
        self._fetch_connections = fetch_connections or zenoh_connection_manager.connections
        self._sync_connected_robots = sync_connected_robots or ros_gateway.sync_connected_robots
        self._devices: list[dict[str, Any]] = []
        self._available = False

    def response(self) -> dict[str, Any]:
        # 네트워크 조회 없이 마지막 결과의 복사본을 돌려준다. 첫 조회 전이나 조회 실패 중이면 unavailable을 붙인다.
        response: dict[str, Any] = {"devices": [device.copy() for device in self._devices]}
        if not self._available:
            response["status"] = "unavailable"
        return response

    async def refresh(self) -> dict[str, Any]:
        # 연결 목록을 별도 스레드에서 조회한 뒤 실제 로봇 목록에 반영한다.
        try:
            # Zenoh REST 조회는 블로킹 urlopen이라 이벤트 루프에서 직접 부르면 주행 루프까지 멈춘다.
            devices = await asyncio.to_thread(self._fetch_connections)
        except ConnectionError:
            # 조회 실패는 로봇이 끊겼다는 근거가 아니다. 연결 상태를 바꾸지 않고 마지막 결과를 유지한다.
            self._available = False
            return self.response()

        self._sync_connected_robots(devices)
        self._devices = [device.copy() for device in devices]
        self._available = True
        return self.response()


connection_sync_service = ConnectionSyncService()
