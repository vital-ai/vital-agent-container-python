import asyncio
import logging
from vital_agent_container.handler.aimp_message_handler_inf import AIMPMessageHandlerInf


class AIMPMessageProcessor:

    def __init__(self):
        pass

    async def process_message(self, handler: AIMPMessageHandlerInf, app_config, client, websocket, data, started_event):

        logger = logging.getLogger("VitalAgentContainerLogger")

        logger.info(f"Processing: {data}")

        try:
            await handler.process_message(app_config, client, websocket, data, started_event)
        except asyncio.CancelledError as e:
            # log canceling
            logger.error(f"Canceling {e}")
            raise e
        except Exception as e:
            logger.error(f"Exception {e}")
            raise e


