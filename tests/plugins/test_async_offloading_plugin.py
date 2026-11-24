"""Unit tests for AsyncOffloadingPlugin.

The plugin base must keep network/integration I/O off the synchronous
SystemEventBus.emit path: handle_runtime_event only enqueues; a background
drain task awaits deliver() per event. A slow or erroring plugin can never
block emit, and sustained backpressure drops the oldest event.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable

import pytest
from orca.events.execution_context import WorkflowExecutionContext
from orca.events.runtime_event import RuntimeEvent

from tests.plugins.conftest import CountingExecutor

from orca.plugins.async_offloading import AsyncOffloadingPlugin


pytestmark = pytest.mark.asyncio


def _make_event(event_name: str = "thread.t-1.STARTED") -> RuntimeEvent:
    return RuntimeEvent(
        event_name=event_name,
        execution_id="exec-1",
        timestamp=time.time(),
        entity_type="thread",
        entity_id="t-1",
        status="STARTED",
        context=WorkflowExecutionContext(
            execution_id="exec-1", workflow_name="wf",
        ),
    )


class _RecordingPlugin(AsyncOffloadingPlugin):
    def __init__(self, queue_size: int = 256) -> None:
        super().__init__(queue_size=queue_size, stop_drain_timeout=5.0)
        self.delivered: list[RuntimeEvent] = []

    async def deliver(self, event: RuntimeEvent) -> None:
        self.delivered.append(event)


async def test_event_delivered_off_the_emit_path(
    wait_until: Callable[..., Awaitable[None]],
) -> None:
    plugin = _RecordingPlugin()
    plugin.start()

    plugin.handle_runtime_event(_make_event())

    await wait_until(lambda: len(plugin.delivered) == 1)
    assert plugin.delivered[0].event_name == "thread.t-1.STARTED"
    await plugin.stop()


async def test_handle_runtime_event_does_not_block_on_slow_deliver(
    wait_until: Callable[..., Awaitable[None]],
) -> None:
    # enqueue is synchronous and never awaits deliver, so a blocked deliver
    # cannot stall the emit path; the drain delivers once unblocked.
    gate = asyncio.Event()

    class _GatedPlugin(AsyncOffloadingPlugin):
        def __init__(self) -> None:
            super().__init__(queue_size=256, stop_drain_timeout=5.0)
            self.delivered: list[RuntimeEvent] = []

        async def deliver(self, event: RuntimeEvent) -> None:
            await gate.wait()
            self.delivered.append(event)

    plugin = _GatedPlugin()
    plugin.start()

    for i in range(5):
        plugin.handle_runtime_event(_make_event(f"thread.t-{i}.STARTED"))
    # No await has yielded to the drain and deliver is gated: nothing delivered.
    assert plugin.delivered == []

    gate.set()
    await wait_until(lambda: len(plugin.delivered) == 5)
    await plugin.stop()


async def test_full_queue_drops_oldest(
    wait_until: Callable[..., Awaitable[None]],
) -> None:
    # Block the drain task so the queue fills, then assert the oldest event
    # is evicted rather than the producer blocking.
    gate = asyncio.Event()

    class _BlockingPlugin(AsyncOffloadingPlugin):
        def __init__(self) -> None:
            super().__init__(queue_size=2, stop_drain_timeout=5.0)
            self.delivered: list[str] = []

        async def deliver(self, event: RuntimeEvent) -> None:
            await gate.wait()
            self.delivered.append(event.event_name)

    plugin = _BlockingPlugin()
    plugin.start()

    # First event is pulled by the drain task and parked on gate.wait().
    plugin.handle_runtime_event(_make_event("thread.t-0.STARTED"))
    await wait_until(lambda: plugin._queue.empty())
    # These two fill the size-2 queue; the third evicts the oldest queued one.
    plugin.handle_runtime_event(_make_event("thread.t-1.STARTED"))
    plugin.handle_runtime_event(_make_event("thread.t-2.STARTED"))
    plugin.handle_runtime_event(_make_event("thread.t-3.STARTED"))

    gate.set()
    await wait_until(lambda: len(plugin.delivered) == 3)
    await plugin.stop()

    # t-1 (oldest queued behind the in-flight t-0) was dropped.
    assert "thread.t-1.STARTED" not in plugin.delivered
    assert "thread.t-3.STARTED" in plugin.delivered


async def test_deliver_exception_does_not_kill_drain_loop(
    wait_until: Callable[..., Awaitable[None]],
) -> None:
    class _FlakyPlugin(AsyncOffloadingPlugin):
        def __init__(self) -> None:
            super().__init__(queue_size=256, stop_drain_timeout=5.0)
            self.delivered: list[str] = []

        async def deliver(self, event: RuntimeEvent) -> None:
            if event.event_name == "boom":
                raise RuntimeError("integration down")
            self.delivered.append(event.event_name)

    plugin = _FlakyPlugin()
    plugin.start()

    plugin.handle_runtime_event(_make_event("boom"))
    plugin.handle_runtime_event(_make_event("thread.t-9.STARTED"))

    await wait_until(lambda: "thread.t-9.STARTED" in plugin.delivered)
    await plugin.stop()

    assert plugin.delivered == ["thread.t-9.STARTED"]


async def test_start_is_idempotent() -> None:
    plugin = _RecordingPlugin()
    first = plugin.start()
    second = plugin.start()
    assert first is second
    await plugin.stop()


async def test_stop_abandons_backlog_does_not_drain_into_slow_target(
    wait_until: Callable[..., Awaitable[None]],
) -> None:
    # A down endpoint must not stall shutdown: stop() returns promptly and
    # abandons the queued backlog rather than draining it.
    gate = asyncio.Event()

    class _StuckPlugin(AsyncOffloadingPlugin):
        def __init__(self) -> None:
            super().__init__(queue_size=256, stop_drain_timeout=0.3)
            self.delivered = 0

        async def deliver(self, event: RuntimeEvent) -> None:
            await gate.wait()
            self.delivered += 1

    plugin = _StuckPlugin()
    plugin.start()
    for i in range(10):
        plugin.handle_runtime_event(_make_event(f"thread.t-{i}.STARTED"))
    # Drain pulls the first event and parks on the gate; nine stay queued.
    await wait_until(lambda: plugin._queue.qsize() == 9)

    # stop() must return promptly (a hang trips the wait_for ceiling) and leave
    # the backlog abandoned rather than drained into the gated target.
    await asyncio.wait_for(plugin.stop(), timeout=5.0)
    assert plugin.delivered == 0


async def test_stop_cancels_an_in_flight_slow_delivery(
    wait_until: Callable[..., Awaitable[None]],
) -> None:
    class _HangingPlugin(AsyncOffloadingPlugin):
        def __init__(self) -> None:
            super().__init__(queue_size=256, stop_drain_timeout=0.2)

        async def deliver(self, event: RuntimeEvent) -> None:
            await asyncio.sleep(30)

    plugin = _HangingPlugin()
    plugin.start()
    plugin.handle_runtime_event(_make_event())
    # Drain pulled the event and is parked inside the 30s delivery.
    await wait_until(lambda: plugin._queue.empty())

    # stop() must cancel the in-flight delivery and return far under 30s; the
    # wait_for ceiling guards against a hang.
    await asyncio.wait_for(plugin.stop(), timeout=5.0)
    assert plugin._drain_task is None


async def test_an_idle_drain_occupies_no_worker_thread(
    counting_default_executor: CountingExecutor,
) -> None:
    """A drain waiting for work must hold no thread while it waits.

    Waiting by handing a blocking `queue.get()` to the executor holds one
    thread per plugin, and cancellation cannot reach a thread that is
    already running: a drain abandoned at shutdown then keeps that thread
    forever, and closing the loop joins it.
    """
    plugin = _RecordingPlugin()

    task = plugin.start()
    await asyncio.sleep(0)
    assert counting_default_executor.submissions == 0

    await plugin.stop()
    assert task.done()
