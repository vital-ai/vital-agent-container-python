"""WebSocket wire contract between the agent container and its clients.

See README "WebSocket protocol". Close codes are the contract; reason strings are informational.
"""
from enum import IntEnum
from typing import Any, Dict, Optional


# WebSocket subprotocol a client must offer when connecting; the server accepts with it, and a
# client should check it was selected (a pre-v2 server selects none)
SUBPROTOCOL = "vital-agent.v2"

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
    UNSUPPORTED_PROTOCOL = 4002  # client did not offer SUBPROTOCOL
    HANDLER_TIMEOUT = 4008    # handler exceeded WS_TASK_TIMEOUT_SECONDS


# Control messages are JSON objects; AIMP payloads are JSON lists
CONTROL_ERROR = "sdk.error"
CONTROL_STATUS = "sdk.status"

ERROR_BUSY = "busy"              # a handler is running; wait for its done / interrupted
ERROR_CANCELLING = "cancelling"  # interrupted handlers still running; wait for status ready
STATUS_DONE = "done"
STATUS_READY = "ready"           # sent after a cancelling rejection, once requests are accepted again
STATUS_INTERRUPTED = "interrupted"
STATUS_IDLE = "idle"

# AIMP objects carry their URI under this key; control messages echo the request's URI
URI_PROPERTY = "URI"


def control_error(error: str, detail: str, in_reply_to: Optional[str] = None) -> Dict[str, Any]:
    return {"type": CONTROL_ERROR, "error": error, "detail": detail, "in_reply_to": in_reply_to}


def control_status(status: str, in_reply_to: Optional[str] = None) -> Dict[str, Any]:
    return {"type": CONTROL_STATUS, "status": status, "in_reply_to": in_reply_to}
