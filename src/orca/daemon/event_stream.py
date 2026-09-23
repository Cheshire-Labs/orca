"""SSE event sink for the daemon.

A single `SseEventSink` lives on `app.state.event_sink` for the daemon's
lifetime (created in `create_app`, persists across load/unload cycles).

- POST /mount-topology registers the sink on the new SystemRuntime via
  `runtime.register_sink(sink)`. From then on, runtime events arrive at
  `sink.on_event(event)`.
- GET /events/stream opens a new subscriber: the sink allocates a bounded
  `asyncio.Queue` and returns both the queue and an unregister callback.
  The route yields events off the queue until the client disconnects,
  then calls the callback to drop the queue.
- `on_event` may be called on-loop (the forwarder) OR off-loop (incident
  records fire from driver-callback threads). It marshals onto the loop via
  `deliver_on_loop`, then fans out via `put_nowait`; on QueueFull it drops the
  oldest event and logs a warning (a slow subscriber can't back-pressure the
  runtime).

Per-connection queue isolation means one disconnected or slow subscriber
cannot affect any other.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

from orca.events.runtime_event import RuntimeEvent
from orca.runtime.interfaces import IEventSink
from orca.runtime.loop_safe import deliver_on_loop


logger = logging.getLogger(__name__)


DEFAULT_SUBSCRIBER_QUEUE_SIZE = 1000


@dataclass
class Subscription:
    """Handle returned by `SseEventSink.subscribe`.

    Callers `await queue.get()` for the next event; call `unsubscribe()`
    when the connection closes (typically in a `finally` block).
    """
    queue: asyncio.Queue[RuntimeEvent]
    unsubscribe: Callable[[], None]


class SseEventSink(IEventSink):
    """Fan-out sink: `on_event` pushes to every subscribed queue.

    The subscriber queues are loop-bound `asyncio.Queue`s, so `on_event`
    marshals onto the daemon loop via `deliver_on_loop` (the bus emits
    off-loop for incidents). The loop is captured on the first `subscribe`.
    """

    def __init__(self, queue_size: int = DEFAULT_SUBSCRIBER_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[RuntimeEvent]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> Subscription:
        self._loop = asyncio.get_running_loop()
        q: asyncio.Queue[RuntimeEvent] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(q)

        def unsubscribe() -> None:
            self._subscribers.discard(q)

        return Subscription(queue=q, unsubscribe=unsubscribe)

    def on_event(self, event: RuntimeEvent) -> None:
        """Fan out to every subscribed queue, on-loop. Safe off-loop: the
        actual fan-out is marshaled onto the loop. On QueueFull for any
        subscriber, drop that subscriber's oldest event and enqueue the new
        one, so a slow subscriber can't back-pressure the runtime."""
        deliver_on_loop(self._loop, self._fan_out, event)

    def _fan_out(self, event: RuntimeEvent) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                _drop_oldest_and_enqueue(q, event)


def _drop_oldest_and_enqueue(
    q: asyncio.Queue[RuntimeEvent], event: RuntimeEvent,
) -> None:
    """Helper: evict the oldest item and push `event`. Logs a warning."""
    try:
        dropped = q.get_nowait()
    except asyncio.QueueEmpty:
        # Extremely unlikely race (queue was full then drained between the
        # put_nowait and this handler), but defensively don't enqueue if
        # the queue is somehow empty -- try put_nowait first; if still
        # full, give up on this event.
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("SSE subscriber queue stayed full; dropped event")
        return
    logger.warning(
        "SSE subscriber queue overflow -- dropped event %s (id=%s)",
        dropped.event_name, dropped.entity_id,
    )
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:
        logger.warning("SSE subscriber queue still full after eviction")
