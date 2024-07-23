import asyncio
import httpx


class RequestTask:
    def __init__(self):
        self._request: httpx.Response = None
        self._event: asyncio.Event = None
        self._task: asyncio.Task = None

    @property
    def request(self):
        return self._request

    @property
    def task(self):
        return self._task

    @property
    def event(self):
        return self._event
