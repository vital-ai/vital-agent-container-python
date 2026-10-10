# Vital Agent Container Python SDK

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

A Python SDK that provides infrastructure components for building AI agent applications with WebSocket-based communication and asynchronous message processing.

## Features

- 🚀 **FastAPI-based WebSocket server** with real-time communication
- 🔄 **Asynchronous message processing** with task management
- 🛡️ **Production-ready** with structured logging and health checks
- 🔌 **Plugin architecture** for custom message handlers
- ☁️ **Cloud-ready** with flexible deployment options
- 📊 **Streaming support** for real-time data flows
- ⚡ **Task cancellation** and interruption handling

## Installation

### Install from PyPI (when published)

```bash
pip install vital-agent-container-sdk
```

### Install from Source

#### Using Conda (Recommended for Development)

```bash
# Clone the repository
git clone https://github.com/vital-ai/vital-agent-container-python.git
cd vital-agent-container-python

# Create environment from environment.yml
conda env create -f environment.yml
conda activate vital-agent-container

# Install in development mode
pip install -e .
```

#### Using pip

```bash
# Clone the repository
git clone https://github.com/vital-ai/vital-agent-container-python.git
cd vital-agent-container-python

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install in development mode
pip install -e .
```



## Configuration

### Option A: File-based configuration

1. Copy the configuration template:
   ```bash
   cp agent_config.yaml.template agent_config.yaml
   ```

2. Pass `app_home` when creating the app:
   ```python
   app = AgentContainerApp(handler, app_home=".")
   ```

### Option B: Programmatic configuration

Pass a config dict directly — no YAML file needed:
```python
config = {"key": "value"}
app = AgentContainerApp(handler, app_config=config)
```

### Environment variables

1. Copy the environment template:
   ```bash
   cp .env.example .env
   ```

2. Update the file with your specific configuration.

### WebSocket protocol

The container owns the WebSocket. Handlers never close it: either side **signals** that it's done,
and the container does the actual close.

**Connecting.** Clients must offer the subprotocol **`vital-agent.v2`**
(`vital_agent_container.protocol.SUBPROTOCOL`). The container accepts with it. A client that doesn't
offer it is closed straight away with **4002** `Unsupported protocol`. Clients should also check that
the server *selected* `vital-agent.v2`: a pre-v2 server selects none, and talking v2 to it would just
hang. A mismatch therefore fails at connect time on either side. (In JWT header mode the token is
still validated first; see below.)

- **A connection carries any number of interactions, one at a time.** A handler finishing does not
  end the connection.
- **The service ends it** with `connection.request_close()` in the handler. The container closes
  with 1000 once the handler has returned, so every reply has been sent first.
- **The client ends it** by sending a message whose first object has
  `"http://vital.ai/ontology/vital-aimp#hasIntent": "close"`. A running handler finishes first.
  A plain WebSocket close or disconnect from the client cancels a running handler immediately.
- **Every interaction ends with exactly one terminal signal:** an `sdk.status` `done` (the handler
  returned; the connection stays open), an `sdk.status` `interrupted`, or a close code. Until one
  arrives, more replies may follow.
- **The container always reads**, even while a handler runs:

| Client sends | No handler running | Handler running |
|---|---|---|
| an AIMP message (JSON list) | starts the handler; when it returns: `done`, or close 1000 if it called `request_close()` | rejected with `busy` |
| intent `interrupt` | `idle` | `interrupted` (the handler is cancelled); the connection stays open |
| intent `close` | close 1000 | close 1000 after the handler returns (no `done`) |
| invalid JSON / not an AIMP list / a binary frame | close 1003 | close 1003 (handler cancelled) |

**Two kinds of rejection, two client reactions.** After `busy`, wait for the running
interaction's `done` or `interrupted`. Both are guaranteed to arrive. After `cancelling`, wait for
`ready`, which is sent exactly once when a request would be accepted again.

**What is a control message.** A control message is a JSON object whose `type` is `sdk.status` or
`sdk.error` (`protocol.is_control_message`). Everything else the client receives is **handler
output**: AIMP message lists, and any JSON objects the handler defines, such as chunk frames
(`{"aimp_chunk": ...}`) for large replies. Clients must classify by `type`, not by "is it an
object", or they'll swallow handler output. Handlers must not send objects with those two `type`
values. Handler output always arrives before the interaction's `done`.

`in_reply_to` is a `URI`
taken from a request's first AIMP object (`null` if it has none). **Which request it names depends
on the message:**

| Control message | `in_reply_to` names |
|---|---|
| `done` | the request whose interaction just ended |
| `interrupted` | the **interrupted request**, not the `interrupt` message |
| `idle` | the `interrupt` message itself (there was nothing to interrupt) |
| `busy`, `cancelling` | the rejected message |
| `ready` | nothing (`null`) |

```json
{"type": "sdk.status", "status": "done",        "in_reply_to": "urn:..."}
{"type": "sdk.status", "status": "interrupted", "in_reply_to": "urn:..."}
{"type": "sdk.status", "status": "idle",        "in_reply_to": "urn:..."}
{"type": "sdk.error",  "error": "busy", "detail": "a message is already being processed", "in_reply_to": "urn:..."}
{"type": "sdk.error",  "error": "cancelling", "detail": "previous requests are still being cancelled", "in_reply_to": "urn:..."}
{"type": "sdk.status", "status": "ready",       "in_reply_to": null}
```

**Messages after a close request aren't processed.** Once the handler has called
`request_close()` and returned, the connection closes. A message that arrives at the same moment is
dropped without a reply. Clients shouldn't send after the service may have ended the interaction
with a close request; wait for `done` first.

**Races a client may see.** Both are harmless, and clients should accept them:

- **`done` then `idle`:** the client sent `interrupt` just as the handler finished. The interaction
  ended normally (`done`); the interrupt then found nothing to cancel (`idle`).
- **`done` then a 1000 close:** the client sent `close` just as the handler finished. `done` ends
  the interaction; the close request is then handled while idle.

`close` followed by `interrupt` (while a handler runs) gives `interrupted` and then a 1000 close.

**A client that stops reading is disconnected.** Control messages (`done`, `busy`, ...) must be
sent within 5s (`CONTROL_SEND_TIMEOUT_SECONDS`), including any wait behind a handler send that's
stuck because the client isn't reading. If not, the container logs a WARNING and closes with
**1008** `Client not reading`, not 1011, because the server didn't fail.

**Keep large handler frames well within that limit.** A control message waits for any handler
frame already being sent. An `interrupt` during a large chunked reply therefore can't be answered
with `interrupted` until the current frame is out. If one frame takes longer than 5s to send (a slow
link, or a client that's slow to read), the connection is closed with 1008. Size chunk frames for
the slowest expected link: for example, a ~700 KB frame needs a sustained ~150 KB/s or better.
Don't raise a chunk-size setting without checking this.

**JWT enforcement is unchanged by this protocol.** Header mode validates the token before the
connection is accepted. Payload mode checks the JWT on **every** message, including `interrupt` and
`close`, *before* the message is parsed. So in payload mode a message without a valid JWT closes with
4001, even if it isn't valid JSON.

**Close codes.** Clients match on the code (`vital_agent_container.protocol.CloseCode`). Reasons are
informational.

| Code | When |
|---|---|
| 1000 | closed on request (`Closed by service` / `Closed by client request`), or `Idle timeout` |
| 4008 | `Handler timeout`: the handler exceeded `WS_TASK_TIMEOUT_SECONDS` |
| 1011 | `Handler error` (the handler raised), or an unexpected container error |
| 1003 | the message was not valid JSON, not an AIMP message list, or a binary frame |
| 1008 | `Client not reading`: a control message couldn't be sent within 5s |
| 4002 | the client didn't offer the `vital-agent.v2` subprotocol |
| 4001 | JWT payload mode rejected a message. In **header mode** a bad or missing token is rejected *before* the connection is accepted, so the client sees the handshake fail with **HTTP 403**, not a 4001 close. |
| 1001 | `Server shutting down` |

**Handler rules:**

- Send with `await connection.send_text(...)` / `send_json(...)`. The `connection` belongs to one
  run. Once that run is over (returned, interrupted, timed out) or the connection has ended, its
  sends raise `ConnectionClosed`. So a handler can't send into a later interaction, including from
  background tasks it started.
- `connection.state` is a snapshot taken when the run starts (JWT payload/tokens etc.). Messages
  that arrive during the run don't change it.
- Return when the interaction is done. Call `connection.request_close()` first if the connection
  should end.
- A cancelled handler (interrupt, client disconnect, deadline, shutdown) gets 5 seconds
  (`HANDLER_UNWIND_GRACE_SECONDS`) to unwind. After that it's logged at ERROR and abandoned. It keeps
  running in the background until it finishes. Don't catch `CancelledError` to keep working.
  After an interrupt the unwind happens in the background. `interrupted` is sent at once, and the
  next message can start a new run while the old one is still unwinding. A connection may have at
  most **one** interrupted handler still running alongside a new one. Beyond that, new requests get
  `sdk.error cancelling` until an interrupted handler finishes (abandoned ones included). Then the
  container sends **one** `sdk.status ready`, and requests are accepted again. A handler that ignores
  cancellation therefore holds up its own connection, not the process. The cap is per connection;
  abandoned handlers across the process are only logged, with a running count.
- While a `ready` is owed no handler is running, so the idle timeout applies. If an interrupted
  handler never finishes, the client gets `1000 Idle timeout` instead of `ready`. Clients should
  treat that as "open a new connection", not as an error.
- On the deadline the container closes with 4008 *before* cancelling the handler, even if a handler
  send is stuck (it waits at most 1s for it). A handler can't
  replace that with its own reply, so enforce a shorter deadline in the handler if you want to answer
  timeouts yourself.

**Timeouts:**

| Env var | Constructor kwarg | Default | Effect |
|---|---|---|---|
| `WS_IDLE_TIMEOUT_SECONDS` | `ws_idle_timeout` | `300` | Closes with 1000 `Idle timeout` after this long with no handler running. Never cancels a handler. |
| `WS_TASK_TIMEOUT_SECONDS` | `ws_task_timeout` | `600` | Closes with 4008 and cancels a handler that runs longer than this. Logged at ERROR. |

- **Values:** plain seconds (`90`, `2.5`). Anything else (e.g. `5m`), or a negative number, raises
  `ValueError` when the app is constructed.
- **Disabling:** only an explicit `0` turns a timeout off, and it logs a WARNING at startup. An empty
  or unset variable uses the default.
- **Precedence:** a constructor kwarg overrides the env var.

`WS_TASK_TIMEOUT_SECONDS` is a backstop. Handlers should still enforce their own, shorter deadlines
so they can send a proper error response.

**The task deadline only bounds async work.** It cancels the handler (after closing with 4008), and a
cancellation can only land while the handler is awaiting something. A handler stuck in blocking
synchronous code (a sync HTTP or database client, CPU-bound work, `time.sleep`) is not interrupted.
That code also blocks the event loop, so every connection's timers in the process stall until it
returns. Run blocking calls with `asyncio.to_thread(...)` (or in an executor). Then the event
loop keeps running and the deadline can fire, though the thread itself runs to completion in the
background. Callers should keep their own timeouts rather than relying on the server's deadline.

Python can't forcibly stop a thread, so bounding blocking work is the handler's job:

1. **Put a timeout on every blocking call.** This is the main fix: e.g. `requests.get(..., timeout=30)`,
   database connect and statement timeouts, client-library timeouts. The call then ends on its own.
   A call with no timeout that hangs holds a thread-pool slot forever. The default pool has
   `min(32, cpu_count + 4)` threads, so enough of these starve every later `to_thread` call in the
   process.
2. **Use cooperative cancellation for long loops or CPU-bound work.** Pass a `threading.Event` into
   the sync code, check it between steps, and set it when the handler is cancelled
   (`except asyncio.CancelledError: stop.set(); raise`).
3. **Use a process instead of a thread** (`ProcessPoolExecutor` or a subprocess you manage) for work
   that must be killable.

## Usage

### Basic Implementation

Create a custom message handler by implementing the `AIMPMessageHandlerInf` interface:

```python
import json
from vital_agent_container.handler.aimp_message_handler_inf import AIMPMessageHandlerInf
from vital_agent_container.agent_container_app import AgentContainerApp

class MyMessageHandler(AIMPMessageHandlerInf):
    async def process_message(self, *, app_config, client, connection, data,
                              jwt_payload=None, jwt_token=None):
        message = json.loads(data)

        # Perform your agent logic
        response = await self.handle_agent_request(message)

        # Reply through the connection
        await connection.send_json(response)

        # Optional: end the connection after this interaction
        connection.request_close()

    async def handle_agent_request(self, message):
        # Implement your specific agent logic here
        return [{"type": "message", "content": "Agent response"}]

# In your application's main module:
def create_agent_app():
    handler = MyMessageHandler()
    return AgentContainerApp(handler, app_home=".")

# Or pass configuration directly as a dict:
def create_agent_app_with_config():
    handler = MyMessageHandler()
    config = {"key": "value"}  # Your configuration dict
    return AgentContainerApp(handler, app_config=config)

# Your application can then run the agent container
if __name__ == "__main__":
    import uvicorn
    app = create_agent_app()
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

### Message Format

Applications using this library will receive messages in the following format through the WebSocket connection:

```json
[{
  "type": "message",
  "http://vital.ai/ontology/vital-aimp#hasIntent": "process",
  "content": "Your message content"
}]
```

Your message handler implementation should parse and respond to these messages according to your agent's logic.

## Development

### Setup Development Environment

```bash
# Install with development dependencies
make install-dev

# Or manually
pip install -e ".[dev]"
pre-commit install
```

### Available Make Commands

```bash
make help           # Show available commands
make test           # Run tests
make test-cov       # Run tests with coverage
make lint           # Run linting
make format         # Format code
make build          # Build package
```

### Code Quality

This project uses:
- **Black** for code formatting
- **isort** for import sorting
- **flake8** for linting
- **mypy** for type checking
- **pre-commit** hooks for automated checks

## API Endpoints

- `GET /health` - Health check endpoint
- `WebSocket /ws` - Main WebSocket endpoint for message processing

## Architecture

```
├── vital_agent_container/
│   ├── agent_container_app.py      # Main FastAPI application and connection loop
│   ├── connection.py               # AgentConnection (handler-facing), ConnectionClosed
│   ├── protocol.py                 # Wire contract: close codes, intents, control messages
│   ├── handler/                    # Message handler interfaces
│   ├── processor/                  # Message processing logic
│   ├── tasks/                      # Task management
│   ├── streaming/                  # Streaming response handling
│   └── utils/                      # Utility functions
├── agent_config.yaml               # Agent configuration
├── environment.yml                 # Conda environment
└── pyproject.toml                  # Modern Python project config
```

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature-name`
3. Make your changes and add tests
4. Run the test suite: `make test`
5. Format your code: `make format`
6. Submit a pull request

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

## Support

For questions and support, please open an issue on the [GitHub repository](https://github.com/vital-ai/vital-agent-container-python).