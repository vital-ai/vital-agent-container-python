import asyncio
import logging
from typing import Dict
import httpx
from vital_agent_container.streaming.custom_streaming_response import CustomStreamingResponse
from vital_agent_container.tasks.request_task import RequestTask


class TaskManagerAsyncClient(httpx.AsyncClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._tasks: Dict[RequestTask, str] = {}
        self._ws_active = None

    @property
    def ws_active(self):
        return self._ws_active

    async def send(self, request, *args, **kwargs):
        is_streaming = kwargs.get("stream", False)

        async def task_coro(rt: RequestTask):
            response = await super(TaskManagerAsyncClient, self).send(request, *args, **kwargs)
            if is_streaming:
                end_event = asyncio.Event()
                custom_response = CustomStreamingResponse(response, end_event)
                rt._event = end_event
                rt._request = custom_response
                return custom_response
            else:
                return response

        request_task = RequestTask()
        task = asyncio.create_task(task_coro(request_task))
        request_task._task = task
        self._tasks[request_task] = request.url.path

        try:
            return await task
        finally:
            # self._tasks.pop(task, None)
            pass

    def log_current_tasks(self):
        logger = logging.getLogger("VitalAgentContainerLogger")
        if self._tasks:
            logger.info(f"Current tasks: {', '.join(self._tasks.values())}")
        else:
            logger.info("No current tasks.")

    async def cancel_current_tasks(self):
        if self._tasks:
            for rt in self._tasks.keys():
                # res = rt.request
                t = rt.task
                status = t.cancel()
                if status:
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
            self._tasks.clear()
        else:
            pass
