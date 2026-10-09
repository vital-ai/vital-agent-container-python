import asyncio
import json
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState


class ConnectionClosed(Exception):
    """Raised by AgentConnection sends once the connection has ended."""


class AgentConnection:
    """A handler's view of its WebSocket connection.

    The container owns the socket: handlers send through this object and signal that the
    connection should end with request_close(); they never close the socket themselves.
    """

    def __init__(self, websocket: WebSocket, client_info: str):
        self._websocket = websocket
        self._send_lock = asyncio.Lock()
        self._close_requested = False
        self._closed = False
        self.client_info = client_info

    @property
    def state(self):
        """Per-connection state, e.g. jwt_payload / jwt_token(s) set by JWT enforcement."""
        return self._websocket.state

    @property
    def close_requested(self) -> bool:
        return self._close_requested

    @property
    def is_open(self) -> bool:
        return (not self._closed
                and self._websocket.application_state == WebSocketState.CONNECTED
                and self._websocket.client_state == WebSocketState.CONNECTED)

    def request_close(self):
        """Close the connection (code 1000) once the current handler run has returned."""
        self._close_requested = True

    async def send_text(self, text: str):
        async with self._send_lock:
            if not self.is_open:
                raise ConnectionClosed("connection has ended")
            try:
                await self._websocket.send_text(text)
            except (RuntimeError, WebSocketDisconnect) as e:
                self._closed = True
                raise ConnectionClosed("connection has ended") from e

    async def send_json(self, obj: Any):
        await self.send_text(json.dumps(obj))

    async def _close(self, code: int, reason: str = ""):
        """Container-only: close the socket if it is still open. Never raises."""
        async with self._send_lock:
            if self.is_open:
                try:
                    await self._websocket.close(code=code, reason=reason)
                except Exception:
                    pass
            self._closed = True
