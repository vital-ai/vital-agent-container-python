"""WebSocket wire contract between the agent container and its clients.

See README "WebSocket protocol". Close codes are the contract; reason strings are informational.
"""
from enum import IntEnum
from typing import Any, Dict


# Intents a client can put on the first AIMP object of a message
INTENT_PROPERTY = "http://vital.ai/ontology/vital-aimp#hasIntent"
INTENT_INTERRUPT = "interrupt"
INTENT_CLOSE = "close"


class CloseCode(IntEnum):
    NORMAL = 1000             # closed on request (service or client), or idle timeout
    GOING_AWAY = 1001         # server shutting down
    UNSUPPORTED_DATA = 1003   # message is not valid JSON or not an AIMP message list
    INTERNAL_ERROR = 1011     # handler raised, or an unexpected SDK error
    UNAUTHORIZED = 4001       # JWT enforcement rejected the connection
    HANDLER_TIMEOUT = 4008    # handler exceeded WS_TASK_TIMEOUT_SECONDS


# Control messages are JSON objects; AIMP payloads are JSON lists
CONTROL_ERROR = "sdk.error"
CONTROL_STATUS = "sdk.status"

ERROR_BUSY = "busy"
STATUS_INTERRUPTED = "interrupted"
STATUS_IDLE = "idle"


def control_error(error: str, detail: str) -> Dict[str, Any]:
    return {"type": CONTROL_ERROR, "error": error, "detail": detail}


def control_status(status: str) -> Dict[str, Any]:
    return {"type": CONTROL_STATUS, "status": status}
