import asyncio
import gc
import json
import sys
import tracemalloc
import uvicorn
import logging
from logging.handlers import RotatingFileHandler
from pythonjsonlogger import jsonlogger
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from vital_agent_container.tasks.task_manager_async_client import TaskManagerAsyncClient
from vital_agent_container.utils.aws_utils import AWSUtils
from vital_agent_container.utils.config_utils import ConfigUtils
from vital_agent_container.processor.aimp_message_processor import AIMPMessageProcessor


logger = logging.getLogger("VitalAgentContainerLogger")
logger.setLevel(logging.INFO)

formatter = jsonlogger.JsonFormatter('%(asctime)s %(name)s %(levelname)s %(message)s')

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)

# log_file = "/var/log/agentcontainer/app.log"

log_file = "app.log"

file_handler = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=5)
file_handler.setFormatter(formatter)

logger.addHandler(stream_handler)
logger.addHandler(file_handler)

service_identifier = AWSUtils.get_task_arn()


class AgentContainerApp(FastAPI):
    def __init__(self, handler, app_home, **kwargs):
        super().__init__(**kwargs)
        self.handler = handler
        self.app_home = app_home
        load_dotenv()
        self.config = ConfigUtils.load_config(app_home)
        self.message_processor = AIMPMessageProcessor()
        self.add_routes()

    async def process_ws_message(self, client: httpx.AsyncClient, websocket: WebSocket, data: str,
                                 started_event: asyncio.Event):

        logger.info(f"process_ws_message: Processing: {data}")

        await self.message_processor.process_message(self.handler, self.config, client, websocket, data, started_event)

    def add_routes(self):
        @self.get("/health")
        async def health_check():
            logger.info("health check")
            return {"status": "ok"}

        @self.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            logger.info(f"WebSocket connection from {websocket.client.host}:{websocket.client.port} accepted.")
            await websocket.accept()
            client = TaskManagerAsyncClient()
            client._ws_active = True
            background_tasks = []
            try:
                while True:
                    data = await websocket.receive_text()

                    logger.info(f"Received message from {websocket.client.host}:{websocket.client.port}: {data}")
                    message_obj = json.loads(data)
                    logger.info(f"Received message: {message_obj}")
                    message_type = message_obj[0].get("type", None)
                    message_intent = message_obj[0].get("http://vital.ai/ontology/vital-aimp#hasIntent", None)
                    logger.info(f"message_type: {message_type}")
                    logger.info(f"message_intent: {message_intent}")

                    if message_intent == "interrupt":
                        logger.info("Processing interrupted by client.")
                        client.log_current_tasks()
                        await client.cancel_current_tasks()
                        for task in background_tasks:
                            task.cancel()
                        try:
                            await asyncio.gather(*background_tasks, return_exceptions=True)
                        except Exception as e:
                            logger.error(f"An error occurred in gather: {e}")
                        try:
                            if client.ws_active:
                                await websocket.close()
                                client._ws_active = False
                        except Exception as e:
                            logger.error(f"An error occurred in interrupt websocket close: {e}")
                        break

                    if len(background_tasks) > 0:
                        logger.info("currently processing task, ignoring new request.")
                        await websocket.send_text("processing task. ignoring message.")
                    else:
                        logger.info(f"Processing message: {data}")
                        started_event = asyncio.Event()
                        task = asyncio.create_task(self.process_ws_message(client, websocket, data, started_event))
                        background_tasks.append(task)
                        await started_event.wait()
                        logger.info(f"Completed Processing message: {data}")
                        # break out of infinite loop
                        break
            except WebSocketDisconnect:
                logger.info("WebSocket connection closed by the client.")
                # still try to close it in finally?
                # client._ws_active = False
            except Exception as e:
                logger.error(f"An error occurred in ws main loop: {e}")
                try:
                    await websocket.close()
                    client._ws_active = False
                except Exception as e2:
                    logger.error(f"An error occurred in websocket close: {e2}")
            finally:
                try:
                    await asyncio.gather(*background_tasks)
                except Exception as e:
                    logger.error(f"An error occurred in final gather: {e}")
                try:
                    if client.ws_active:
                        await websocket.close()
                except Exception as e3:
                    logger.error(f"An error occurred in finally websocket close: {e3}")
                await client.cancel_current_tasks()

        @self.on_event("shutdown")
        async def shutdown_event():
            logger.info("Shutting down...")
