"""The live event feed both servers serve at `/ws/events`.

Each subscriber has its own bounded queue and names one execution or every
execution. `on_event` may be called off the loop (incidents fire from
driver-callback threads), so the fan-out is marshalled onto the loop. When a
queue is full its oldest frame is dropped, so a slow client cannot stall the
runtime.
"""

import asyncio
import json
import logging
import threading
from dataclasses import dataclass

from orca.api.interfaces import IEventFeed, IEventSubscriber
from orca.events.runtime_event import RuntimeEvent
from orca.runtime.loop_safe import deliver_on_loop

logger = logging.getLogger(__name__)

SUBSCRIBER_QUEUE_SIZE = 1000


@dataclass
class _Subscriber:
    subscriber: IEventSubscriber
    execution_id: str | None
    queue: asyncio.Queue[str]


class EventFeed(IEventFeed):
    """Fans every RuntimeEvent out to the connected subscribers as JSON text."""

    def __init__(self, queue_size: int = SUBSCRIBER_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._subscribers: dict[int, _Subscriber] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def subscribe(self, subscriber: IEventSubscriber, execution_id: str | None) -> None:
        self._loop = asyncio.get_running_loop()
        entry = _Subscriber(subscriber, execution_id, asyncio.Queue(maxsize=self._queue_size))
        with self._lock:
            self._subscribers[id(subscriber)] = entry

    def unsubscribe(self, subscriber: IEventSubscriber) -> None:
        with self._lock:
            self._subscribers.pop(id(subscriber), None)

    async def run_subscriber(self, subscriber: IEventSubscriber) -> None:
        with self._lock:
            entry = self._subscribers.get(id(subscriber))
        if entry is None:
            return
        while True:
            frame = await entry.queue.get()
            try:
                await subscriber.send_text(frame)
            except Exception as exc:
                logger.info("event subscriber disconnected: %s", exc)
                self.unsubscribe(subscriber)
                return

    def on_event(self, event: RuntimeEvent) -> None:
        deliver_on_loop(self._loop, self._fan_out, event)

    def _fan_out(self, event: RuntimeEvent) -> None:
        with self._lock:
            subscribers = list(self._subscribers.values())
        if not subscribers:
            return
        frame = json.dumps(event.to_dict(), default=str)
        for entry in subscribers:
            if entry.execution_id in (None, event.execution_id):
                _enqueue_dropping_oldest(entry.queue, frame)


def _enqueue_dropping_oldest(queue: asyncio.Queue[str], frame: str) -> None:
    if queue.full():
        queue.get_nowait()
        logger.warning("event subscriber queue overflow; dropped the oldest frame")
    queue.put_nowait(frame)
