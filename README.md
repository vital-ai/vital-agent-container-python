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

### WebSocket connection lifecycle

A connection can carry any number of messages, one at a time. Either side may close the
connection. A handler that closes the socket when it's done is a normal end and is logged at INFO.

When a handler closes the socket, the connection ends. A handler that keeps working after
`close()` (persisting, logging, other cleanup) gets **5 seconds** to finish. After that it's
cancelled. Do slow post-close work in a separate task you manage yourself. The 5s grace period is a
fixed constant (`SERVER_CLOSE_GRACE_SECONDS` in `agent_container_app.py`), not configurable like the
timeouts below.

The container doesn't read the next message until the handler sets `started_event`. When the
handler sets it decides what happens next:

- **Set early** (when work starts): the container keeps reading while the handler runs. A message
  that arrives in that time is rejected with `processing task. ignoring message.`, and an interrupt
  message cancels the running handler.
- **Set at the end** (e.g. in a `finally`): the container doesn't read during the handler run. A
  new message waits until the handler finishes, and an interrupt can't arrive mid-run.

Two timeouts bound how long a connection stays open:

| Env var | Constructor kwarg | Default | Effect |
|---|---|---|---|
| `WS_IDLE_TIMEOUT_SECONDS` | `ws_idle_timeout` | `300` | Closes the connection (code 1000, `Idle timeout`) after this long with no messages and no handler running. A running handler is never cancelled by this timeout. |
| `WS_TASK_TIMEOUT_SECONDS` | `ws_task_timeout` | `600` | Cancels a handler that runs longer than this and closes the connection (code 1011, `Handler timeout`). Logged at ERROR. |

Set either one to `0` (or empty) to disable it. A constructor kwarg overrides the env var. Values
are plain seconds (`90`, `2.5`). Anything else (e.g. `5m`) raises `ValueError` when the app is
constructed.
`WS_TASK_TIMEOUT_SECONDS` is a backstop. Handlers should still enforce their own, shorter deadlines
so they can send a proper error response.

## Usage

### Basic Implementation

Create a custom message handler by implementing the `AIMPMessageHandlerInf` interface:

```python
from vital_agent_container.handler.aimp_message_handler_inf import AIMPMessageHandlerInf
from vital_agent_container.agent_container_app import AgentContainerApp

class MyMessageHandler(AIMPMessageHandlerInf):
    async def process_message(self, config, client, websocket, data, started_event):
        # Your custom message processing logic here
        # Process the incoming message data
        message = json.loads(data)
        
        # Perform your agent logic
        response = await self.handle_agent_request(message)
        
        # Send response back through websocket
        await websocket.send_text(json.dumps(response))
        
        # Signal that processing has started
        started_event.set()
    
    async def handle_agent_request(self, message):
        # Implement your specific agent logic here
        return {"status": "processed", "result": "Agent response"}

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
│   ├── agent_container_app.py      # Main FastAPI application
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