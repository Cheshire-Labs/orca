"""Which device locks a request took while it was in flight, for the check that such a route declares `drives_device`.

Each request runs in its own context, and a task it starts inherits that context, so a lock taken by the request
or by a task it started is counted until its answer is sent. A run that outlives its submit counts for no later request.
"""

import itertools
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from orca.resource_models.tracked_lock import TrackedLock

_request: ContextVar[int | None] = ContextVar("surface_request", default=None)
_ids = itertools.count(1)
_in_flight: dict[int, set[str]] = {}
_finished: dict[int, set[str]] = {}
_last: list[int] = []
_original: Callable[[TrackedLock, str], AbstractAsyncContextManager[None]] | None = None


def _record_on_take() -> None:
    global _original
    if _original is not None:
        return
    original = TrackedLock.held_for
    _original = original

    def held_for(self: TrackedLock, purpose: str) -> AbstractAsyncContextManager[None]:
        request = _request.get()
        if request is not None and request in _in_flight:
            _in_flight[request].add(self.name)
        return original(self, purpose)

    setattr(TrackedLock, "held_for", held_for)


class DeviceLockRecorder:
    """ASGI wrapper: gives each HTTP request an id and files its locks before its headers are sent."""

    def __init__(self, app: ASGIApp) -> None:
        _record_on_take()
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request = next(_ids)
        _in_flight[request] = set()
        token = _request.set(request)

        def file_it() -> None:
            if request not in _finished:
                _finished[request] = _in_flight[request]
                _last.append(request)

        async def filing_send(message: Message) -> None:
            # Filed before the headers go: a 204 is a whole answer to the client once they arrive.
            if message["type"] == "http.response.start":
                file_it()
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                _in_flight.pop(request, None)
            await send(message)

        try:
            await self._app(scope, receive, filing_send)
        finally:
            _request.reset(token)
            file_it()
            _in_flight.pop(request, None)


def locks_taken_by_the_last_request() -> set[str]:
    return set(_finished.get(_last[-1], set())) if _last else set()
