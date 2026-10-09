"""WebSocket connection-lifecycle tests: normal close, multi-message, idle timeout, task deadline.

See planning/websocket-connection-lifecycle-hardening.md.
"""
import asyncio
import gc
import json
import logging
import threading
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import jwt
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect, WebSocketState

from vital_agent_container import agent_container_app
from vital_agent_container.agent_container_app import AgentContainerApp
from vital_agent_container.handler.aimp_message_handler_inf import AIMPMessageHandlerInf


LOGGER_NAME = "VitalAgentContainerLogger"
SECRET = "test-secret-key"
NOT_CONNECTED = "WebSocket is not connected"


# ── Test handler and app ─────────────────────────────────────

class ScriptedHandler(AIMPMessageHandlerInf):
    """Runs `behavior(websocket, started_event)` for every message."""

    def __init__(self, behavior):
        self.behavior = behavior
        self.finally_ran = threading.Event()

    async def process_message(self, *, websocket, started_event, **kwargs):
        try:
            await self.behavior(websocket, started_event)
        finally:
            self.finally_ran.set()


class ObservedApp(AgentContainerApp):
    """Signals when a connection's cleanup has finished, so tests can assert on logs."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.connection_done = threading.Event()

    async def _cleanup_websocket_resources(self, *args, **kwargs):
        try:
            await super()._cleanup_websocket_resources(*args, **kwargs)
        finally:
            self.connection_done.set()


# Handler behaviors

async def close_then_signal(ws, started):
    """cardiff's pattern: reply, close, then signal (the original bug trigger)."""
    await ws.send_text("reply")
    await ws.close()
    started.set()


async def signal_then_close(ws, started):
    started.set()
    await asyncio.sleep(0.05)
    await ws.send_text("reply")
    await ws.close()


async def reply_only(ws, started):
    await ws.send_text("reply")
    started.set()


async def slow_reply(ws, started):
    started.set()
    await asyncio.sleep(0.6)
    await ws.send_text("reply")


async def hang_without_signal(ws, started):
    await asyncio.Event().wait()


async def hang_then_close_in_finally(ws, started):
    try:
        await asyncio.Event().wait()
    finally:
        await ws.close()
        started.set()


async def signal_then_hang(ws, started):
    started.set()
    await asyncio.Event().wait()


async def close_signal_then_raise(ws, started):
    """cardiff's error path: reply, close and signal in finally, then re-raise."""
    try:
        raise ValueError("handler blew up")
    finally:
        await ws.send_text("reply")
        await ws.close()
        started.set()


post_close_done = threading.Event()


async def close_signal_then_short_work(ws, started):
    await ws.send_text("reply")
    await ws.close()
    started.set()
    await asyncio.sleep(0.1)  # e.g. persisting after the reply
    post_close_done.set()


async def close_signal_then_hang(ws, started):
    await ws.send_text("reply")
    await ws.close()
    started.set()
    await asyncio.Event().wait()


async def hang_swallowing_cancel(ws, started):
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        return  # swallowed: the deadline's TimeoutError never propagates


async def signal_then_raise(ws, started):
    await ws.send_text("reply")
    started.set()
    raise ValueError("handler blew up")


# ── JWT modes ────────────────────────────────────────────────

def _token(sub="test-user"):
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": sub, "exp": now + timedelta(hours=1), "iat": now}, SECRET, algorithm="HS256")


def _jwt_config(mode):
    if mode == "disabled":
        return {"enabled": False}
    return {
        "enabled": True,
        "secret_key": SECRET,
        "algorithm": "HS256",
        "enforcement_mode": mode,
        "required_claims": ["sub", "exp", "iat"],
        "custom_claims": [],
    }


def _connect_kwargs(mode):
    if mode == "header":
        return {"headers": {"authorization": f"Bearer {_token()}"}}
    return {}


def _message(mode, intent=None):
    obj = {"type": "test"}
    if intent:
        obj["http://vital.ai/ontology/vital-aimp#hasIntent"] = intent
    if mode == "payload":
        obj["http://vital.ai/ontology/vital-aimp#hasJwtJSON"] = json.dumps({"jwt_token": _token()})
    return json.dumps([obj])


def make_app(behavior, mode="disabled", **timeouts):
    handler = ScriptedHandler(behavior)
    app = ObservedApp(handler, app_config={}, jwt_config=_jwt_config(mode), **timeouts)
    return app, handler


def wait_done(app, timeout=5):
    assert app.connection_done.wait(timeout), "connection was not cleaned up"


def receive_close(ws):
    """Next message must be a server close; return (code, reason)."""
    msg = ws.receive()
    assert msg["type"] == "websocket.close", msg
    return msg.get("code"), msg.get("reason")


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    return caplog


def errors(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER_NAME and r.levelno >= logging.ERROR]


def infos(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER_NAME and r.levelno == logging.INFO]


ALL_MODES = ["disabled", "header", "payload"]


# ── Gap 3: no ERROR on normal close ──────────────────────────

@pytest.mark.parametrize("mode", ALL_MODES)
def test_handler_close_before_signal_logs_no_error(logs, mode):
    app, handler = make_app(close_then_signal, mode)
    with TestClient(app).websocket_connect("/ws", **_connect_kwargs(mode)) as ws:
        ws.send_text(_message(mode))
        assert ws.receive_text() == "reply"
        assert receive_close(ws)[0] == 1000
        wait_done(app)

    assert errors(logs) == []
    assert any("closed by server" in m for m in infos(logs))


def test_handler_close_after_signal_logs_no_error(logs):
    app, _ = make_app(signal_then_close)
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text(_message("disabled"))
        assert ws.receive_text() == "reply"
        assert receive_close(ws)[0] == 1000
        ws.close()
        wait_done(app)

    assert errors(logs) == []


@pytest.mark.parametrize("mode", ALL_MODES)
def test_client_close_logs_disconnected_normally(logs, mode):
    app, _ = make_app(reply_only, mode)
    with TestClient(app).websocket_connect("/ws", **_connect_kwargs(mode)) as ws:
        ws.send_text(_message(mode))
        assert ws.receive_text() == "reply"
        ws.close()
        wait_done(app)

    assert errors(logs) == []
    assert any("disconnected normally" in m for m in infos(logs))


def test_genuine_fault_still_logs_error(logs):
    app, _ = make_app(reply_only)

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    with patch.object(app, "_process_message_with_intent_handling", boom):
        with TestClient(app).websocket_connect("/ws") as ws:
            ws.send_text(_message("disabled"))
            assert receive_close(ws) == (1011, "Internal server error")
            wait_done(app)

    assert any("WebSocket communication error" in m and "boom" in m for m in errors(logs))


def test_invalid_json_has_no_trailing_not_connected_error(logs):
    app, _ = make_app(reply_only)
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text("not json")
        assert receive_close(ws) == (1003, "Invalid JSON")
        wait_done(app)

    errs = errors(logs)
    assert any("Invalid JSON" in m for m in errs)
    assert not any(NOT_CONNECTED in m or "communication error" in m for m in errs)


def test_interrupt_cancels_in_flight_handler(logs):
    app, handler = make_app(signal_then_hang)
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text(_message("disabled"))
        ws.send_text(_message("disabled", intent="interrupt"))
        assert handler.finally_ran.wait(5)
        ws.close()
        wait_done(app)

    assert not any(NOT_CONNECTED in m or "communication error" in m for m in errors(logs))


# ── Gap 1: pruning / multi-message ───────────────────────────

@pytest.mark.parametrize("mode", ALL_MODES)
def test_multi_message_connection_processes_every_message(logs, mode):
    app, _ = make_app(reply_only, mode)
    with TestClient(app).websocket_connect("/ws", **_connect_kwargs(mode)) as ws:
        for _ in range(3):
            ws.send_text(_message(mode))
            assert ws.receive_text() == "reply"
        ws.close()
        wait_done(app)

    assert errors(logs) == []
    assert any("after 3 messages" in m for m in infos(logs))


def test_message_during_in_flight_task_is_rejected(logs):
    app, _ = make_app(slow_reply)
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text(_message("disabled"))
        ws.send_text(_message("disabled"))
        assert ws.receive_text() == "processing task. ignoring message."
        assert ws.receive_text() == "reply"
        ws.close()
        wait_done(app)


def test_each_failed_task_is_logged_exactly_once(logs):
    app, _ = make_app(signal_then_raise)
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text(_message("disabled"))
        assert ws.receive_text() == "reply"
        ws.send_text(_message("disabled"))  # the loop prunes the first failed task
        assert ws.receive_text() == "reply"
        ws.close()
        wait_done(app)
    gc.collect()

    # One per message: the first is pruned by the loop, the second by cleanup
    failed = [m for m in errors(logs) if "Handler task failed" in m]
    assert len(failed) == 2 and all("handler blew up" in m for m in failed)
    assert not any("never retrieved" in r.getMessage() for r in logs.records)


def test_failed_task_is_logged_when_handler_closed_the_socket(logs):
    app, _ = make_app(close_signal_then_raise)
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text(_message("disabled"))
        assert ws.receive_text() == "reply"
        assert receive_close(ws)[0] == 1000
        wait_done(app)

    failed = [m for m in errors(logs) if "Handler task failed" in m]
    assert len(failed) == 1 and "handler blew up" in failed[0]
    assert not any(NOT_CONNECTED in m or "communication error" in m for m in errors(logs))


# ── Close grace period ───────────────────────────────────────

def test_post_close_work_within_grace_completes(logs):
    post_close_done.clear()
    app, handler = make_app(close_signal_then_short_work)
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text(_message("disabled"))
        assert ws.receive_text() == "reply"
        assert receive_close(ws)[0] == 1000
        wait_done(app)

    assert post_close_done.is_set()
    assert errors(logs) == []


def test_post_close_work_beyond_grace_is_cancelled(logs, monkeypatch):
    monkeypatch.setattr(agent_container_app, "SERVER_CLOSE_GRACE_SECONDS", 0.2)
    app, handler = make_app(close_signal_then_hang)
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text(_message("disabled"))
        assert ws.receive_text() == "reply"
        assert receive_close(ws)[0] == 1000
        wait_done(app)

    assert handler.finally_ran.is_set()
    # AIMPMessageProcessor logs every cancellation as "Canceling" at ERROR (pre-existing)
    assert [m for m in errors(logs) if not m.startswith("Canceling")] == []


async def test_cancel_during_grace_wait_still_cleans_up():
    app, _ = make_app(reply_only)
    websocket = MagicMock()
    websocket.application_state = WebSocketState.DISCONNECTED  # loop exits as "closed by server"
    websocket.client_state = WebSocketState.DISCONNECTED

    async def cancelled_grace(background_tasks):
        raise asyncio.CancelledError  # server shutting down mid-wait

    cleaned_up = []

    async def cleanup(*args, **kwargs):
        cleaned_up.append(True)

    with patch.object(app, "_let_closing_tasks_finish", cancelled_grace), \
            patch.object(app, "_cleanup_websocket_resources", cleanup):
        with pytest.raises(asyncio.CancelledError):
            await app._run_receive_loop(websocket, "test", get_jwt=None)

    assert cleaned_up == [True]


# ── Gap 2: idle timeout ──────────────────────────────────────

@pytest.mark.parametrize("mode", ALL_MODES)
def test_idle_connection_is_closed(logs, mode):
    app, _ = make_app(reply_only, mode, ws_idle_timeout=0.2)
    with TestClient(app).websocket_connect("/ws", **_connect_kwargs(mode)) as ws:
        assert receive_close(ws) == (1000, "Idle timeout")
        wait_done(app)

    assert errors(logs) == []
    assert any("Idle timeout" in m for m in infos(logs))


def test_idle_timeout_after_last_message(logs):
    app, _ = make_app(reply_only, ws_idle_timeout=0.2)
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text(_message("disabled"))
        assert ws.receive_text() == "reply"
        assert receive_close(ws) == (1000, "Idle timeout")
        wait_done(app)

    assert any("Idle timeout" in m and "after 1 messages" in m for m in infos(logs))


def test_idle_timeout_does_not_cancel_running_handler(logs):
    app, handler = make_app(slow_reply, ws_idle_timeout=0.2)
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text(_message("disabled"))
        assert ws.receive_text() == "reply"  # handler outlived several idle periods
        assert receive_close(ws) == (1000, "Idle timeout")
        wait_done(app)

    assert errors(logs) == []


# ── Gap 4: per-task deadline ─────────────────────────────────

@pytest.mark.parametrize("mode", ALL_MODES)
def test_hung_handler_is_cancelled_and_closed_1011(logs, mode):
    app, handler = make_app(hang_without_signal, mode, ws_task_timeout=0.2)
    with TestClient(app).websocket_connect("/ws", **_connect_kwargs(mode)) as ws:
        ws.send_text(_message(mode))
        assert receive_close(ws) == (1011, "Handler timeout")
        wait_done(app)

    assert handler.finally_ran.is_set()
    errs = errors(logs)
    assert any("deadline" in m for m in errs)
    assert not any(NOT_CONNECTED in m or "communication error" in m for m in errs)


def test_hung_handler_closing_in_finally_is_not_closed_twice(logs):
    app, _ = make_app(hang_then_close_in_finally, ws_task_timeout=0.2)
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text(_message("disabled"))
        assert receive_close(ws)[0] == 1000  # the handler's own close wins
        wait_done(app)

    assert not any(NOT_CONNECTED in m or "communication error" in m or "Error closing" in m
                   for m in errors(logs) + infos(logs))


def test_deadline_fires_even_if_handler_swallows_cancellation(logs):
    app, _ = make_app(hang_swallowing_cancel, ws_task_timeout=0.2)
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text(_message("disabled"))
        assert receive_close(ws) == (1011, "Handler timeout")
        wait_done(app)

    assert any("deadline" in m for m in errors(logs))


def test_hung_handler_after_signal_is_closed_by_deadline(logs):
    app, _ = make_app(signal_then_hang, ws_idle_timeout=0.2, ws_task_timeout=0.4)
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text(_message("disabled"))
        assert receive_close(ws) == (1011, "Handler timeout")
        ws.close()
        wait_done(app)


async def test_handler_own_timeout_error_is_not_swallowed():
    async def own_deadline(ws, started):
        raise TimeoutError("handler's own deadline")

    app, _ = make_app(own_deadline, ws_task_timeout=10)
    started = asyncio.Event()
    with pytest.raises(TimeoutError, match="handler's own deadline"):
        await app.process_ws_message(websocket=MagicMock(), data="[]", started_event=started)
    assert started.is_set()


# ── Configuration ────────────────────────────────────────────

def test_timeout_defaults(monkeypatch):
    monkeypatch.delenv("WS_IDLE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("WS_TASK_TIMEOUT_SECONDS", raising=False)
    app, _ = make_app(reply_only)
    assert app.ws_idle_timeout == 300
    assert app.ws_task_timeout == 600


def test_timeouts_from_env(monkeypatch):
    monkeypatch.setenv("WS_IDLE_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("WS_TASK_TIMEOUT_SECONDS", "")
    app, _ = make_app(reply_only)
    assert app.ws_idle_timeout == 45
    assert app.ws_task_timeout is None


def test_kwargs_override_env_and_zero_disables(monkeypatch):
    monkeypatch.setenv("WS_IDLE_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("WS_TASK_TIMEOUT_SECONDS", "45")
    app, _ = make_app(reply_only, ws_idle_timeout=0, ws_task_timeout=12)
    assert app.ws_idle_timeout is None
    assert app.ws_task_timeout == 12


def test_invalid_env_timeout_fails_with_clear_error(monkeypatch):
    monkeypatch.setenv("WS_IDLE_TIMEOUT_SECONDS", "5m")
    with pytest.raises(ValueError, match="WS_IDLE_TIMEOUT_SECONDS must be a number of seconds"):
        make_app(reply_only)
