"""A subscriber that keeps the frames an event stream sends it."""

import asyncio
import contextlib
from collections.abc import AsyncIterator

from orca.api.interfaces import IEventStream


class RecordingSubscriber:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[str] = asyncio.Queue()
        self.fail_sends = False

    async def send_text(self, data: str) -> None:
        if self.fail_sends:
            raise ConnectionError("gone")
        self.frames.put_nowait(data)


@contextlib.asynccontextmanager
async def sending(stream: IEventStream, subscriber: RecordingSubscriber) -> AsyncIterator[asyncio.Task[None]]:
    """Run `stream.run_subscriber` for an already subscribed `subscriber` until the block ends."""
    task = asyncio.create_task(stream.run_subscriber(subscriber))
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
