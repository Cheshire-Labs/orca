"""Tests for api/event_feed.py. Each test targets a specific bug-shape.

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
import json

import pytest

from orca.api.event_feed import EventFeed
from orca.events.execution_context import WorkflowExecutionContext
from orca.events.runtime_event import RuntimeEvent
from tests.event_subscriber import RecordingSubscriber, sending


def _event(tag: str, execution_id: str = "exec-1") -> RuntimeEvent:
    """Minimal RuntimeEvent for fanout assertions. Fields are placeholders
    except event_name, which acts as a tag the test can recognize."""
    return RuntimeEvent(
        event_name=tag,
        execution_id=execution_id,
        timestamp=0.0,
        entity_type="ENTITY",
        entity_id="id",
        status="RUNNING",
        context=WorkflowExecutionContext(execution_id="wf", workflow_name="wf"),
    )


def _name_of(frame: str) -> str:
    return json.loads(frame)["event_name"]


async def _next(subscriber: RecordingSubscriber) -> str:
    return _name_of(await asyncio.wait_for(subscriber.frames.get(), timeout=1.0))


async def test_subscribe_receives_on_event() -> None:
    """Fanout basics: an event posted to on_event reaches the subscriber."""
    sink = EventFeed()
    subscriber = RecordingSubscriber()
    sink.subscribe(subscriber, None)
    async with sending(sink, subscriber):
        sink.on_event(_event("tag-1"))
        assert await _next(subscriber) == "tag-1"


async def test_the_execution_id_filter_keeps_only_that_execution() -> None:
    sink = EventFeed()
    subscriber = RecordingSubscriber()
    sink.subscribe(subscriber, "watched")
    sink.on_event(_event("other", execution_id="elsewhere"))
    sink.on_event(_event("mine", execution_id="watched"))
    async with sending(sink, subscriber):
        assert await _next(subscriber) == "mine"
    assert subscriber.frames.empty()


async def test_unsubscribe_removes_queue_from_fanout() -> None:
    """After unsubscribe, events posted to on_event must NOT reach that subscriber.

    A regression where unsubscribe is a no-op would silently leak queues
    (memory + per-event work grows with every closed connection).
    """
    sink = EventFeed()
    subscriber = RecordingSubscriber()
    sink.subscribe(subscriber, None)
    assert sink.subscriber_count == 1

    sink.unsubscribe(subscriber)
    assert sink.subscriber_count == 0

    sink.on_event(_event("after-unsub"))
    async with sending(sink, subscriber) as task:
        await asyncio.wait_for(task, timeout=1.0)
    assert subscriber.frames.empty()


async def test_queue_overflow_drops_oldest_and_accepts_new(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fill the queue past capacity; oldest event must be evicted, newest kept.

    Catches a regression where the QueueFull branch silently drops the NEW
    event instead of the oldest (or does nothing, back-pressuring on_event).
    """
    sink = EventFeed(queue_size=3)
    subscriber = RecordingSubscriber()
    sink.subscribe(subscriber, None)

    # Fill + overflow: push 4 events into a size-3 queue before anything is sent.
    for i in range(4):
        sink.on_event(_event(f"e{i}"))

    async with sending(sink, subscriber):
        received = [await _next(subscriber) for _ in range(3)]
    assert received == ["e1", "e2", "e3"]
    assert any("overflow" in r.message.lower() for r in caplog.records), (
        "overflow was not logged"
    )


async def test_one_full_subscriber_does_not_affect_others() -> None:
    """Subscription isolation: if one queue is full and dropping, a second
    subscriber with capacity must still receive the event.

    Catches a regression where overflow handling bleeds across subscribers
    (e.g., touching the wrong queue or raising past QueueFull).
    """
    sink = EventFeed(queue_size=1)
    slow, fast = RecordingSubscriber(), RecordingSubscriber()
    sink.subscribe(slow, None)
    sink.subscribe(fast, None)

    async with sending(sink, fast):
        # The first event fills the slow queue; the fast one is sent at once.
        sink.on_event(_event("first"))
        assert await _next(fast) == "first"
        # The second event: the slow queue is full and drops its oldest; fast accepts.
        sink.on_event(_event("second"))
        assert await _next(fast) == "second"
    async with sending(sink, slow):
        assert await _next(slow) == "second"


async def test_run_subscriber_sends_frames_until_a_send_fails() -> None:
    sink = EventFeed()
    subscriber = RecordingSubscriber()
    sink.subscribe(subscriber, None)
    running = asyncio.create_task(sink.run_subscriber(subscriber))

    sink.on_event(_event("delivered"))
    assert await _next(subscriber) == "delivered"

    subscriber.fail_sends = True
    sink.on_event(_event("undeliverable"))
    await asyncio.wait_for(running, timeout=1.0)
    assert sink.subscriber_count == 0


async def test_an_event_no_run_owns_reaches_only_the_subscribers_that_asked_for_every_run() -> None:
    sink = EventFeed()
    everything, one_run = RecordingSubscriber(), RecordingSubscriber()
    sink.subscribe(everything, None)
    sink.subscribe(one_run, "exec-1")
    sink.on_event(_event("COMMAND.shaker_1.COMPLETED", execution_id="_system"))
    async with sending(sink, everything):
        assert await _next(everything) == "COMMAND.shaker_1.COMPLETED"
    assert one_run.frames.empty()
