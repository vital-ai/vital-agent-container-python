import asyncio
from typing import Optional, Dict, Any
import httpx
from starlette.websockets import WebSocket
from abc import ABC, abstractmethod


class AIMPMessageHandlerInf(ABC):

    @abstractmethod
    async def process_message(self, *, app_config: Dict[str, Any], client: httpx.AsyncClient, websocket: WebSocket, data: str, started_event: asyncio.Event, jwt_payload: Optional[Dict[str, Any]] = None, jwt_token: Optional[str] = None):
        pass
