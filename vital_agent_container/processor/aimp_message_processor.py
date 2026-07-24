import asyncio
import logging
from typing import Optional, Dict, Any
import httpx
from starlette.websockets import WebSocket
from vital_agent_container.handler.aimp_message_handler_inf import AIMPMessageHandlerInf


class AIMPMessageProcessor:

    def __init__(self):
        pass

    async def process_message(self, *, handler: AIMPMessageHandlerInf, app_config: Dict[str, Any], client: httpx.AsyncClient, websocket: WebSocket, data: str, started_event: asyncio.Event, jwt_payload: Optional[Dict[str, Any]] = None, jwt_token: Optional[str] = None):

        logger = logging.getLogger("VitalAgentContainerLogger")

        logger.info(f"Processing: {data}")

        try:
            await handler.process_message(
                app_config=app_config,
                client=client,
                websocket=websocket,
                data=data,
                started_event=started_event,
                jwt_payload=jwt_payload,
                jwt_token=jwt_token
            )
        except asyncio.CancelledError as e:
            # log canceling
            logger.error(f"Canceling {e}")
            raise e
        except Exception as e:
            logger.error(f"Exception {e}")
            raise e


