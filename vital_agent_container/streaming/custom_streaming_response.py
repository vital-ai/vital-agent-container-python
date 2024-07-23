import asyncio
import logging
import httpx


class CustomStreamingResponse:
    def __init__(self, response: httpx.Response, end_event: asyncio.Event):
        self._response = response
        self._end_event = end_event

    async def aiter_lines(self):
        try:
            async for line in self._response.aiter_lines():
                yield line
        finally:
            self.handle_end_of_stream()

    async def aiter_bytes(self):
        try:
            async for chunk in self._response.aiter_bytes():
                yield chunk
        finally:
            self.handle_end_of_stream()

    async def aiter_raw(self):
        try:
            async for chunk in self._response.aiter_raw():
                yield chunk
        finally:
            self.handle_end_of_stream()

    def handle_end_of_stream(self):
        self._end_event.set()
        logger = logging.getLogger("VitalAgentContainerLogger")
        logger.info("Reached the end of the stream.")

    def __getattr__(self, name):
        return getattr(self._response, name)
