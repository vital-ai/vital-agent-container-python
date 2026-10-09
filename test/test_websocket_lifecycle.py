"""WebSocket protocol tests (Contract v2): the SDK owns the socket, either side signals close,
the loop always reads, and every outcome has its own close code.

See README "WebSocket protocol" and planning/websocket-connection-lifecycle-hardening.md.
"""
import asyncio
import json
import logging
import threading
from datetime import datetime, timezone, timedelta

import jwt
import pytest
from starlette.testclient import TestClient

from vital_agent_container import agent_container_app
from vital_agent_container.agent_container_app import AgentContainerApp
from vital_agent_container.connection import ConnectionClosed
from vital_agent_container.handler.aimp_message_handler_inf import AIMPMessageHandlerInf
from vital_agent_container.protocol import CloseCode


LOGGER_NAME = "VitalAgentContainerLogger"
SECRET = "test-secret-key"


# ── Test handler and app ─────────────────────────────────────

class ScriptedHandler(AIMPMessageHandlerInf):
    """Runs `behavior(connection, data)` for every message."""

    def __init__(self, behavior):
        self.behavior = behavior
        self.finally_ran = threading.Event()
        self.calls = 0

    async def process_message(self, *, connection, data, **kwargs):
        self.calls += 1
        try:
            await self.behavior(connection, data)
        finally:
            self.finally_ran.set()


class ObservedApp(AgentContainerApp):
    """Signals when a connection has fully ended, so tests can assert on logs."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.connection_done = threading.Event()

    async def _run_connection(self, *args, **kwargs):
        try:
            await super()._run_connection(*args, **kwargs)
        finally:
            self.connection_done.set()


# Handler behaviors

async def reply(connection, data):
    await connection.send_text("reply")


async def reply_and_request_close(connection, data):
    await connection.send_text("reply")
    connection.request_close()


async def slow_reply(connection, data):
    await asyncio.sleep(0.4)
    await connection.send_text("reply")


async def hang_if_asked(connection, data):
    if "hang" in data:
        await asyncio.Event().wait()
    await connection.send_text("reply")


async def hang(connection, data):
    await asyncio.Event().wait()


async def hang_then_send_in_cleanup(connection, data):
    try:
        await asyncio.Event().wait()
    finally:
        await connection.send_text("late")  # raises ConnectionClosed once the SDK has closed


send_errors = []


async def hang_then_record_send_error(connection, data):
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        try:
            await connection.send_text("late")
        except ConnectionClosed as e:
            send_errors.append(e)
        raise


async def raise_error(connection, data):
    await connection.send_text("reply")
    raise ValueError("handler blew up")


async def raise_own_timeout(connection, data):
    raise TimeoutError("handler's own deadline")


slow_unwind_done = threading.Event()
late_send_results = []


async def slow_unwind_then_late_send(connection, data):
    """On "hang": swallow the interrupt, keep working, then try to send. Otherwise reply."""
    if "hang" not in data:
        await connection.send_text("reply")
        return
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await asyncio.sleep(0.5)
        try:
            await connection.send_text("late")
            late_send_results.append("sent")
        except ConnectionClosed:
            late_send_results.append("ConnectionClosed")


seen_subjects = []


async def record_jwt_subject_over_time(connection, data):
    seen_subjects.append(connection.state.jwt_payload["jwt_token"]["sub"])
    await asyncio.sleep(0.3)  # other messages arrive meanwhile
    seen_subjects.append(connection.state.jwt_payload["jwt_token"]["sub"])
    await connection.send_text("reply")


async def hang_then_slow_unwind(connection, data):
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await asyncio.sleep(1.0)  # e.g. a slow cleanup call; swallows the cancellation
        slow_unwind_done.set()


# ── JWT modes and helpers ────────────────────────────────────

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


def _message(mode="disabled", intent=None, text="hello", jwt_in_payload=True, uri="urn:test:msg", sub="test-user"):
    obj = {"type": "test", "text": text, "URI": uri}
    if intent:
        obj["http://vital.ai/ontology/vital-aimp#hasIntent"] = intent
    if mode == "payload" and jwt_in_payload:
        obj["http://vital.ai/ontology/vital-aimp#hasJwtJSON"] = json.dumps({"jwt_token": _token(sub)})
    return json.dumps([obj])


def make_app(behavior, mode="disabled", **timeouts):
    handler = ScriptedHandler(behavior)
    app = ObservedApp(handler, app_config={}, jwt_config=_jwt_config(mode), **timeouts)
    return app, handler


def connect(app, mode="disabled"):
    return TestClient(app).websocket_connect("/ws", **_connect_kwargs(mode))


def wait_done(app, timeout=5):
    assert app.connection_done.wait(timeout), "connection did not end"


def receive_close(ws):
    """Next message must be a server close; return (code, reason)."""
    msg = ws.receive()
    assert msg["type"] == "websocket.close", msg
    return msg.get("code"), msg.get("reason")


def receive_control(ws):
    msg = json.loads(ws.receive_text())
    assert isinstance(msg, dict), msg
    return msg


def expect_done(ws, uri="urn:test:msg"):
    assert receive_control(ws) == {"type": "sdk.status", "status": "done", "in_reply_to": uri}


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    return caplog


def errors(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER_NAME and r.levelno >= logging.ERROR]


def infos(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER_NAME and r.levelno == logging.INFO]


ALL_MODES = ["disabled", "header", "payload"]


# ── Closing: either side signals, the SDK closes ─────────────

@pytest.mark.parametrize("mode", ALL_MODES)
def test_service_request_close_closes_1000_after_reply(logs, mode):
    app, _ = make_app(reply_and_request_close, mode)
    with connect(app, mode) as ws:
        ws.send_text(_message(mode))
        assert ws.receive_text() == "reply"
        assert receive_close(ws) == (CloseCode.NORMAL, "Closed by service")
        wait_done(app)

    assert errors(logs) == []


@pytest.mark.parametrize("mode", ALL_MODES)
def test_client_close_intent_when_idle_closes_1000(logs, mode):
    app, _ = make_app(reply, mode)
    with connect(app, mode) as ws:
        ws.send_text(_message(mode))
        assert ws.receive_text() == "reply"
        expect_done(ws)
        ws.send_text(_message(mode, intent="close"))
        assert receive_close(ws) == (CloseCode.NORMAL, "Closed by client request")
        wait_done(app)

    assert errors(logs) == []


def test_client_close_intent_while_running_waits_for_handler(logs):
    app, _ = make_app(slow_reply)
    with connect(app) as ws:
        ws.send_text(_message())
        ws.send_text(_message(intent="close"))
        assert ws.receive_text() == "reply"  # the running handler finishes first
        assert receive_close(ws) == (CloseCode.NORMAL, "Closed by client request")
        wait_done(app)

    assert errors(logs) == []


def test_handler_finishing_does_not_close_the_connection(logs):
    app, handler = make_app(reply)
    with connect(app) as ws:
        for i in range(3):
            ws.send_text(_message(uri=f"urn:test:msg-{i}"))
            assert ws.receive_text() == "reply"
            expect_done(ws, f"urn:test:msg-{i}")  # each interaction ends with done
        ws.send_text(_message(intent="close"))
        assert receive_close(ws)[0] == CloseCode.NORMAL
        wait_done(app)

    assert handler.calls == 3
    assert errors(logs) == []


def test_client_disconnect_while_running_cancels_handler(logs):
    send_errors.clear()
    app, handler = make_app(hang_then_record_send_error)
    with connect(app) as ws:
        ws.send_text(_message())
        ws.close()
        wait_done(app)

    assert handler.finally_ran.is_set()
    assert len(send_errors) == 1  # sends after the client left raise ConnectionClosed
    assert errors(logs) == []
    assert any("disconnected" in m for m in infos(logs))


def test_client_disconnect_while_idle(logs):
    app, _ = make_app(reply)
    with connect(app) as ws:
        ws.close()
        wait_done(app)

    assert errors(logs) == []


# ── The loop always reads: busy and interrupt ────────────────

def test_message_while_running_gets_busy_control_message(logs):
    app, handler = make_app(slow_reply)
    with connect(app) as ws:
        ws.send_text(_message(uri="urn:test:first"))
        ws.send_text(_message(uri="urn:test:second"))
        assert receive_control(ws) == {"type": "sdk.error", "error": "busy",
                                       "detail": "a message is already being processed",
                                       "in_reply_to": "urn:test:second"}
        assert ws.receive_text() == "reply"
        expect_done(ws, "urn:test:first")
        ws.send_text(_message(intent="close"))
        assert receive_close(ws)[0] == CloseCode.NORMAL
        wait_done(app)

    assert handler.calls == 1


@pytest.mark.parametrize("mode", ALL_MODES)
def test_interrupt_cancels_handler_and_keeps_connection_open(logs, mode):
    app, handler = make_app(hang_if_asked, mode)
    with connect(app, mode) as ws:
        ws.send_text(_message(mode, text="hang", uri="urn:test:long"))
        ws.send_text(_message(mode, intent="interrupt", uri="urn:test:interrupt"))
        # in_reply_to names the interaction that ended
        assert receive_control(ws) == {"type": "sdk.status", "status": "interrupted",
                                       "in_reply_to": "urn:test:long"}
        assert handler.finally_ran.wait(2)

        ws.send_text(_message(mode))  # the connection is still usable
        assert ws.receive_text() == "reply"
        expect_done(ws)
        ws.send_text(_message(mode, intent="close"))
        assert receive_close(ws)[0] == CloseCode.NORMAL
        wait_done(app)

    assert errors(logs) == []


def test_interrupt_unwinds_in_background_and_blocks_late_sends(logs):
    """The loop keeps reading while an interrupted handler unwinds, and the interrupted run can't
    send into the next interaction."""
    late_send_results.clear()
    app, _ = make_app(slow_unwind_then_late_send)
    with connect(app) as ws:
        ws.send_text(_message(text="hang", uri="urn:test:long"))
        ws.send_text(_message(intent="interrupt"))
        assert receive_control(ws)["status"] == "interrupted"

        # Served while the interrupted handler is still unwinding (0.5s)
        ws.send_text(_message(uri="urn:test:next"))
        assert ws.receive_text() == "reply"
        expect_done(ws, "urn:test:next")
        assert late_send_results == []  # the old handler hasn't tried yet

        for _ in range(200):
            if late_send_results:
                break
            threading.Event().wait(0.01)
        assert late_send_results == ["ConnectionClosed"]

        ws.send_text(_message(intent="close"))
        assert receive_close(ws)[0] == CloseCode.NORMAL  # no "late" frame arrived first
        wait_done(app)


def test_payload_mode_run_state_is_not_changed_by_later_messages(logs):
    seen_subjects.clear()
    app, _ = make_app(record_jwt_subject_over_time, "payload")
    with connect(app, "payload") as ws:
        ws.send_text(_message("payload", sub="alice", uri="urn:test:first"))
        ws.send_text(_message("payload", sub="bob", uri="urn:test:second"))  # busy, but carries a JWT
        assert receive_control(ws)["error"] == "busy"
        assert ws.receive_text() == "reply"
        expect_done(ws, "urn:test:first")
        ws.send_text(_message("payload", intent="close"))
        assert receive_close(ws)[0] == CloseCode.NORMAL
        wait_done(app)

    assert seen_subjects == ["alice", "alice"]


def test_interrupt_while_idle_reports_idle(logs):
    app, handler = make_app(reply)
    with connect(app) as ws:
        ws.send_text(_message(intent="interrupt", uri="urn:test:interrupt"))
        assert receive_control(ws) == {"type": "sdk.status", "status": "idle",
                                       "in_reply_to": "urn:test:interrupt"}
        ws.send_text(_message(intent="close"))
        assert receive_close(ws)[0] == CloseCode.NORMAL
        wait_done(app)

    assert handler.calls == 0


# ── Errors ───────────────────────────────────────────────────

def test_handler_error_closes_1011(logs):
    app, _ = make_app(raise_error)
    with connect(app) as ws:
        ws.send_text(_message())
        assert ws.receive_text() == "reply"
        assert receive_close(ws) == (CloseCode.INTERNAL_ERROR, "Handler error")
        wait_done(app)

    failed = [m for m in errors(logs) if "Handler failed" in m]
    assert len(failed) == 1 and "handler blew up" in failed[0]


def test_handler_own_timeout_error_is_a_handler_error_not_4008(logs):
    app, _ = make_app(raise_own_timeout, ws_task_timeout=10)
    with connect(app) as ws:
        ws.send_text(_message())
        assert receive_close(ws) == (CloseCode.INTERNAL_ERROR, "Handler error")
        wait_done(app)


def test_invalid_json_closes_1003(logs):
    app, handler = make_app(reply)
    with connect(app) as ws:
        ws.send_text("not json")
        assert receive_close(ws) == (CloseCode.UNSUPPORTED_DATA, "Invalid JSON")
        wait_done(app)

    assert handler.calls == 0


def test_non_aimp_message_closes_1003(logs):
    app, handler = make_app(reply)
    with connect(app) as ws:
        ws.send_text(json.dumps({"not": "a list"}))
        assert receive_close(ws) == (CloseCode.UNSUPPORTED_DATA, "Invalid message")
        wait_done(app)

    assert handler.calls == 0


def test_payload_mode_without_jwt_closes_4001(logs):
    app, handler = make_app(reply, "payload")
    with connect(app, "payload") as ws:
        ws.send_text(_message("payload", jwt_in_payload=False))
        assert receive_close(ws) == (CloseCode.UNAUTHORIZED, "JWT token required in message payload")
        wait_done(app)

    assert handler.calls == 0


def test_payload_mode_checks_jwt_before_parsing(logs):
    """JWT handling is unchanged by contract v2: in payload mode a malformed message fails the
    JWT check (4001) before it is parsed (which would give 1003)."""
    app, handler = make_app(reply, "payload")
    with connect(app, "payload") as ws:
        ws.send_text("not json")
        assert receive_close(ws)[0] == CloseCode.UNAUTHORIZED
        wait_done(app)

    assert handler.calls == 0


@pytest.mark.parametrize("intent", ["interrupt", "close"])
def test_payload_mode_requires_jwt_on_control_intents(logs, intent):
    app, _ = make_app(reply, "payload")
    with connect(app, "payload") as ws:
        ws.send_text(_message("payload", intent=intent, jwt_in_payload=False))
        assert receive_close(ws) == (CloseCode.UNAUTHORIZED, "JWT token required in message payload")
        wait_done(app)


# ── Idle timeout ─────────────────────────────────────────────

@pytest.mark.parametrize("mode", ALL_MODES)
def test_idle_connection_is_closed_1000(logs, mode):
    app, _ = make_app(reply, mode, ws_idle_timeout=0.2)
    with connect(app, mode) as ws:
        assert receive_close(ws) == (CloseCode.NORMAL, "Idle timeout")
        wait_done(app)

    assert errors(logs) == []


def test_idle_timer_does_not_run_while_handler_runs(logs):
    app, _ = make_app(slow_reply, ws_idle_timeout=0.2)
    with connect(app) as ws:
        ws.send_text(_message())
        assert ws.receive_text() == "reply"  # the 0.4s handler outlived the 0.2s idle timeout
        expect_done(ws)
        assert receive_close(ws) == (CloseCode.NORMAL, "Idle timeout")
        wait_done(app)

    assert errors(logs) == []


# ── Task deadline ────────────────────────────────────────────

@pytest.mark.parametrize("mode", ALL_MODES)
def test_deadline_closes_4008_and_cancels_handler(logs, mode):
    app, handler = make_app(hang, mode, ws_task_timeout=0.2)
    with connect(app, mode) as ws:
        ws.send_text(_message(mode))
        assert receive_close(ws) == (CloseCode.HANDLER_TIMEOUT, "Handler timeout")
        wait_done(app)

    assert handler.finally_ran.is_set()
    assert any("deadline" in m for m in errors(logs))


def test_deadline_4008_is_sent_before_handler_cleanup_runs(logs):
    app, handler = make_app(hang_then_send_in_cleanup, ws_task_timeout=0.2)
    with connect(app) as ws:
        ws.send_text(_message())
        # The close comes first; the handler's cleanup send fails with ConnectionClosed
        assert receive_close(ws) == (CloseCode.HANDLER_TIMEOUT, "Handler timeout")
        wait_done(app)

    assert handler.finally_ran.is_set()


def test_slow_unwinding_handler_is_abandoned_after_grace(logs, monkeypatch):
    monkeypatch.setattr(agent_container_app, "HANDLER_UNWIND_GRACE_SECONDS", 0.2)
    slow_unwind_done.clear()
    app, _ = make_app(hang_then_slow_unwind, ws_task_timeout=0.2)
    with connect(app) as ws:
        ws.send_text(_message())
        assert receive_close(ws) == (CloseCode.HANDLER_TIMEOUT, "Handler timeout")
        # The connection ends without waiting out the handler's 1s unwind
        wait_done(app, timeout=0.9)
        assert not slow_unwind_done.is_set()
        assert any("abandoning" in m for m in errors(logs))

        # The abandoned task is tracked until it finishes, then forgotten
        assert slow_unwind_done.wait(3)
        for _ in range(100):
            if not app._abandoned_tasks:
                break
            threading.Event().wait(0.01)
        assert not app._abandoned_tasks


def test_deadline_disabled_with_zero(logs):
    app, _ = make_app(slow_reply, ws_task_timeout=0)
    assert app.ws_task_timeout is None
    with connect(app) as ws:
        ws.send_text(_message())
        assert ws.receive_text() == "reply"
        expect_done(ws)
        ws.send_text(_message(intent="close"))
        assert receive_close(ws)[0] == CloseCode.NORMAL
        wait_done(app)


# ── Shutdown ─────────────────────────────────────────────────

class FakeWebSocket:
    """Just enough of a Starlette WebSocket for driving _run_connection directly."""

    def __init__(self, messages):
        from starlette.websockets import WebSocketState
        self.application_state = WebSocketState.CONNECTED
        self.client_state = WebSocketState.CONNECTED
        self._messages = asyncio.Queue()
        for m in messages:
            self._messages.put_nowait(m)
        self.sent = []
        self.closed_with = None
        from starlette.datastructures import State
        self.state = State()

    async def receive_text(self):
        return await self._messages.get()  # blocks once the scripted messages run out

    async def send_text(self, text):
        self.sent.append(text)

    async def close(self, code=1000, reason=""):
        from starlette.websockets import WebSocketState
        self.application_state = WebSocketState.DISCONNECTED
        self.closed_with = (code, reason)


async def test_deadline_close_is_not_blocked_by_a_stuck_send(monkeypatch):
    """A handler stuck sending to a client that stopped reading can't hold up the 4008 close."""
    from vital_agent_container import connection as connection_module
    monkeypatch.setattr(connection_module, "SEND_LOCK_WAIT_ON_CLOSE_SECONDS", 0.1)
    send_started = asyncio.Event()

    async def send_forever(connection, data):
        await connection.send_text("stuck")

    app, handler = make_app(send_forever, ws_task_timeout=0.2)
    websocket = FakeWebSocket([_message()])

    async def stuck_send(text):
        send_started.set()
        await asyncio.Event().wait()  # the client never drains

    websocket.send_text = stuck_send

    async def no_jwt(data):
        return None, None

    await asyncio.wait_for(app._run_connection(websocket, "test", no_jwt), timeout=3)
    assert send_started.is_set()
    assert websocket.closed_with == (CloseCode.HANDLER_TIMEOUT, "Handler timeout")
    assert handler.finally_ran.is_set()


async def test_shutdown_cancels_handler_and_closes_1001():
    app, handler = make_app(hang)
    websocket = FakeWebSocket([_message()])

    async def no_jwt(data):
        return None, None

    connection_task = asyncio.create_task(app._run_connection(websocket, "test", no_jwt))
    for _ in range(100):  # wait until the handler is running
        if handler.calls:
            break
        await asyncio.sleep(0.01)
    connection_task.cancel()  # what the server does on shutdown
    with pytest.raises(asyncio.CancelledError):
        await connection_task

    assert handler.finally_ran.is_set()
    assert websocket.closed_with == (CloseCode.GOING_AWAY, "Server shutting down")


# ── Contract constants ───────────────────────────────────────

def test_close_codes_are_stable():
    """Clients match on these codes; changing one is a breaking change."""
    assert CloseCode.NORMAL == 1000
    assert CloseCode.GOING_AWAY == 1001
    assert CloseCode.UNSUPPORTED_DATA == 1003
    assert CloseCode.INTERNAL_ERROR == 1011
    assert CloseCode.UNAUTHORIZED == 4001
    assert CloseCode.HANDLER_TIMEOUT == 4008


# ── Configuration ────────────────────────────────────────────

def test_timeout_defaults(monkeypatch):
    monkeypatch.delenv("WS_IDLE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("WS_TASK_TIMEOUT_SECONDS", raising=False)
    app, _ = make_app(reply)
    assert app.ws_idle_timeout == 300
    assert app.ws_task_timeout == 600


def test_timeouts_from_env(monkeypatch):
    monkeypatch.setenv("WS_IDLE_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("WS_TASK_TIMEOUT_SECONDS", "0")
    app, _ = make_app(reply)
    assert app.ws_idle_timeout == 45
    assert app.ws_task_timeout is None


def test_empty_env_uses_default(monkeypatch):
    """A template rendering a missing value as empty must not turn the timeouts off."""
    monkeypatch.setenv("WS_IDLE_TIMEOUT_SECONDS", "")
    monkeypatch.setenv("WS_TASK_TIMEOUT_SECONDS", "  ")
    app, _ = make_app(reply)
    assert app.ws_idle_timeout == 300
    assert app.ws_task_timeout == 600


def test_disabled_timeout_logs_warning(logs, monkeypatch):
    monkeypatch.setenv("WS_TASK_TIMEOUT_SECONDS", "0")
    make_app(reply)
    warnings = [r.getMessage() for r in logs.records
                if r.name == LOGGER_NAME and r.levelno == logging.WARNING]
    assert any("WS_TASK_TIMEOUT_SECONDS is 0" in m for m in warnings)


def test_negative_timeout_is_rejected(monkeypatch):
    monkeypatch.setenv("WS_IDLE_TIMEOUT_SECONDS", "-5")
    with pytest.raises(ValueError, match="WS_IDLE_TIMEOUT_SECONDS must be 0"):
        make_app(reply)


def test_kwargs_override_env_and_zero_disables(monkeypatch):
    monkeypatch.setenv("WS_IDLE_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("WS_TASK_TIMEOUT_SECONDS", "45")
    app, _ = make_app(reply, ws_idle_timeout=0, ws_task_timeout=12)
    assert app.ws_idle_timeout is None
    assert app.ws_task_timeout == 12


def test_invalid_env_timeout_fails_with_clear_error(monkeypatch):
    monkeypatch.setenv("WS_IDLE_TIMEOUT_SECONDS", "5m")
    with pytest.raises(ValueError, match="WS_IDLE_TIMEOUT_SECONDS must be a number of seconds"):
        make_app(reply)
