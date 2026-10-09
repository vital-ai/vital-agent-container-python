import asyncio
import logging
from typing import Optional, Dict, Any
import httpx
from vital_agent_container.connection import AgentConnection
from vital_agent_container.handler.aimp_message_handler_inf import AIMPMessageHandlerInf


class AIMPMessageProcessor:

    def __init__(self):
        pass

    async def process_message(self, *, handler: AIMPMessageHandlerInf, app_config: Dict[str, Any], client: httpx.AsyncClient, connection: AgentConnection, data: str, jwt_payload: Optional[Dict[str, Any]] = None, jwt_token: Optional[str] = None):

        logger = logging.getLogger("VitalAgentContainerLogger")

        logger.info(f"Processing: {data}")

        try:
            await handler.process_message(
                app_config=app_config,
                client=client,
                connection=connection,
                data=data,
                jwt_payload=jwt_payload,
                jwt_token=jwt_token
            )
        except asyncio.CancelledError:
            # Cancellation is a normal outcome (interrupt, disconnect, deadline, shutdown)
            logger.info(f"Handler cancelled for {connection.client_info}")
            raise
