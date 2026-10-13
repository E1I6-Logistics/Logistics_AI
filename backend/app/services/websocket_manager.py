"""Frontend WebSocket connection registry without external messaging systems."""

from __future__ import annotations

from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    # ConnectionManager의 초기 상태를 설정한다.
    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []

    # 관련을 연결한다.
    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)

    # 관련을 연결을 끊는다.
    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    # 연결된 웹소켓에 상태를 전송한다.
    async def broadcast(self, message: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                dead.append(connection)
        for connection in dead:
            self.disconnect(connection)


manager = ConnectionManager()
