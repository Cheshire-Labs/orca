"""Tests for daemon/event_stream.py. Each test targets a specific bug-shape.

What these catch (all exercising code I wrote, not the library):

- Queue bounded behavior: if someone removes the `put_nowait`/QueueFull
  branch, a slow subscriber would deadlock the runtime. The overflow test
  forces the drop-oldest path and asserts the queue still accepts new
  events and the oldest one is gone.
- Subscription isolation: dropping events on one slow queue must not
  touch any other subscriber's queue.
- unsubscribe removes the queue from the fanout set. A regression where
  unsubscribe is a no-op would leak queues across load/unload cycles.

Not covered here (tautologies):
- "Putting into an asyncio.Queue delivers a get()" -- stdlib.
"""

import asyncio
import logging

import pytest

from orca.daemon.event_stream import SseEventSink
from orca.events.execution_context import WorkflowExecutionContext
from orca.events.runtime_event import RuntimeEvent


def _event(tag: str) -> RuntimeEvent:
    """Minimal RuntimeEvent for fanout assertions. Fields are placeholders
    except event_name, which acts as a tag the test can recognize."""
    return RuntimeEvent(
        event_name=tag,
        execution_id="exec-1",
        timestamp=0.0,
        entity_type="ENTITY",
        entity_id="id",
        status="RUNNING",
        context=WorkflowExecutionContext(execution_id="wf", workflow_name="wf"),
    )


async def test_subscribe_receives_on_event() -> None:
    """Fanout basics: an event posted to on_event lands on the subscriber's queue."""
    sink = SseEventSink()
    sub = sink.subscribe()
    sink.on_event(_event("tag-1"))
    got = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
    assert got.event_name == "tag-1"


async def test_unsubscribe_removes_queue_from_fanout() -> None:
    """After unsubscribe, events posted to on_event must NOT reach that queue.

    A regression where unsubscribe is a no-op would silently leak queues
    (memory + per-event work grows with every closed SSE connection).
    """
    sink = SseEventSink()
    sub = sink.subscribe()
    assert sink.subscriber_count == 1

    sub.unsubscribe()
    assert sink.subscriber_count == 0

    sink.on_event(_event("after-unsub"))
    # Queue still empty; nothing was enqueued after unsubscribe.
    assert sub.queue.empty()


async def test_queue_overflow_drops_oldest_and_accepts_new(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fill the queue past capacity; oldest event must be evicted, newest kept.

    Catches a regression where the QueueFull branch silently drops the NEW
    event instead of the oldest (or does nothing, back-pressuring on_event).
    """
    sink = SseEventSink(queue_size=3)
    sub = sink.subscribe()

    # Fill + overflow: push 4 events into a size-3 queue.
    for i in range(4):
        sink.on_event(_event(f"e{i}"))

    # Queue should have exactly 3 items now: e1, e2, e3 (e0 evicted).
    received: list[str] = []
    for _ in range(3):
        event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
        received.append(event.event_name)
    assert received == ["e1", "e2", "e3"], (
        f"expected ['e1','e2','e3'], got {received}"
    )
    # Warning logged for the dropped event.
    assert any("overflow" in r.message.lower() for r in caplog.records), (
        "overflow was not logged"
    )


async def test_one_full_subscriber_does_not_affect_others() -> None:
    """Subscription isolation: if one queue is full and dropping, a second
    subscriber with capacity must still receive the event.

    Catches a regression where overflow handling bleeds across subscribers
    (e.g., touching the wrong queue or raising past QueueFull).
    """
    sink = SseEventSink(queue_size=1)
    slow = sink.subscribe()
    fast = sink.subscribe()

    # First event fills both queues.
    sink.on_event(_event("first"))
    # Drain only the fast subscriber.
    await fast.queue.get()
    # Second event: slow queue is full (drops oldest); fast is empty (accepts).
    sink.on_event(_event("second"))

    fast_got = await asyncio.wait_for(fast.queue.get(), timeout=1.0)
    assert fast_got.event_name == "second"
    slow_got = await asyncio.wait_for(slow.queue.get(), timeout=1.0)
    assert slow_got.event_name == "second"  # oldest dropped, new stored
