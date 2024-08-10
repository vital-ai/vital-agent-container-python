import asyncio
import httpx
from starlette.websockets import WebSocket
from abc import ABC, abstractmethod


class AIMPMessageHandlerInf(ABC):

    @abstractmethod
    async def process_message(self, config, client: httpx.AsyncClient, websocket: WebSocket, data: str, started_event: asyncio.Event):
        pass



