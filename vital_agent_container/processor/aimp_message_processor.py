import asyncio

from vital_agent_container.handler.aimp_message_handler_inf import AIMPMessageHandlerInf


class AIMPMessageProcessor:

    def __init__(self):
        pass

    async def process_message(self, handler: AIMPMessageHandlerInf, app_config, client, websocket, data, started_event):
        print(f"Processing: {data}")
        try:
            return await handler.process_message(app_config, client, websocket, data, started_event)
        except asyncio.CancelledError:
            # log canceling
            raise

