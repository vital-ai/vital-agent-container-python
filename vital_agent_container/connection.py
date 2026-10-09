import asyncio
import json
from typing import Any, Callable, Optional

from starlette.datastructures import State
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState


# On close, how long to wait for an in-flight send (e.g. to a client that stopped reading)
# before closing without it, and how long the close itself may take
SEND_LOCK_WAIT_ON_CLOSE_SECONDS = 1.0
CLOSE_TIMEOUT_SECONDS = 5.0


class ConnectionClosed(Exception):
    """Raised by AgentConnection sends once the connection, or the handler's run, has ended."""


class _Channel:
    """Container-only: the socket shared by all runs on one connection."""

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self._send_lock = asyncio.Lock()
        self._closed = False

    @property
    def is_open(self) -> bool:
        return (not self._closed
                and self.websocket.application_state == WebSocketState.CONNECTED
                and self.websocket.client_state == WebSocketState.CONNECTED)

    async def send_text(self, text: str, sender_ended: Optional[Callable[[], bool]] = None):
        async with self._send_lock:
            # Re-checked under the lock: a send that was waiting for it when its run ended
            # (interrupt, deadline) must not go out into the next interaction
            if sender_ended is not None and sender_ended():
                raise ConnectionClosed("this handler run has ended")
            if not self.is_open:
                raise ConnectionClosed("connection has ended")
            try:
                await self.websocket.send_text(text)
            except (RuntimeError, WebSocketDisconnect) as e:
                self._closed = True
                raise ConnectionClosed("connection has ended") from e

    async def close(self, code: int, reason: str = "") -> bool:
        """Close if still open. Never raises and never waits indefinitely behind a stuck send.
        Returns True if a close frame was sent."""
        was_open = self.is_open
        self._closed = True  # later sends fail fast
        if not was_open:
            return False
        try:
            await asyncio.wait_for(self._send_lock.acquire(), SEND_LOCK_WAIT_ON_CLOSE_SECONDS)
            locked = True
        except asyncio.TimeoutError:
            locked = False  # a send is stuck; close without waiting for it
        try:
            await asyncio.wait_for(self.websocket.close(code=code, reason=reason), CLOSE_TIMEOUT_SECONDS)
            return True
        except Exception:
            return False
        finally:
            if locked:
                self._send_lock.release()


class AgentConnection:
    """A handler's view of its connection for one run (one client message).

    The container owns the socket: handlers send through this object and signal that the
    connection should end with request_close(); they never close the socket themselves.
    Once the run is over (returned, interrupted, timed out, or the connection ended),
    sends raise ConnectionClosed.
    """

    def __init__(self, channel: _Channel, client_info: str, state: Optional[State] = None,
                 request_uri: Optional[str] = None):
        self._channel = channel
        self._close_requested = False
        self._ended = False
        self.client_info = client_info
        self.request_uri = request_uri
        self.state = state if state is not None else State()

    @property
    def close_requested(self) -> bool:
        return self._close_requested

    @property
    def is_open(self) -> bool:
        return not self._ended and self._channel.is_open

    def request_close(self):
        """Close the connection (code 1000) once this run has returned."""
        self._close_requested = True

    async def send_text(self, text: str):
        if self._ended:
            raise ConnectionClosed("this handler run has ended")
        await self._channel.send_text(text, sender_ended=lambda: self._ended)

    async def send_json(self, obj: Any):
        await self.send_text(json.dumps(obj))

    def _end(self):
        """Container-only: no more sends from this run."""
        self._ended = True
