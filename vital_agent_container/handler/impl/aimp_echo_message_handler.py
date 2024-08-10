import asyncio
import httpx
from starlette.websockets import WebSocket
from vital_agent_container.handler.aimp_message_handler_inf import AIMPMessageHandlerInf


class AIMPEchoMessageHandler(AIMPMessageHandlerInf):
    async def process_message(self, config, client: httpx.AsyncClient, websocket: WebSocket, data: str, started_event: asyncio.Event):
        try:
            print(f"Received Message: {data}")
            await websocket.send_text(data)
            print(f"Sent Message: {data}")
            # await websocket.close(1000, "Processing Complete")
            # print(f"Websocket closed.")
            started_event.set()
            print(f"Completed Event.")
        except asyncio.CancelledError:
            # log canceling
            raise




