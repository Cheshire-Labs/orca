"""A producer must never raise because the drain's loop is gone.

The plugin's background drain waits on an `asyncio.Event` and is woken with
`loop.call_soon_threadsafe`. Its producer is synchronous and runs on
whatever thread the EventBus dispatched on, which during shutdown can be
after the loop has closed. `call_soon_threadsafe` raises `RuntimeError` on a
closed loop, and that would propagate into `SystemEventBus.emit` -- the exact
path the plugin exists to keep free of I/O and of failures.
"""

import asyncio
import time
from typing import Callable

from orca.events.execution_context import WorkflowExecutionContext
from orca.events.runtime_event import RuntimeEvent

from orca.plugins.async_offloading import AsyncOffloadingPlugin

Drain = tuple[Callable[[], None], Callable[[], None]]
"""How to start one drain, and how to hand it work."""


def _event() -> RuntimeEvent:
    return RuntimeEvent(
        event_name="thread.t-1.STARTED",
        execution_id="exec-1",
        timestamp=time.time(),
        entity_type="thread",
        entity_id="t-1",
        status="STARTED",
        context=WorkflowExecutionContext(execution_id="exec-1", workflow_name="wf"),
    )


class _SilentPlugin(AsyncOffloadingPlugin):
    async def deliver(self, event: RuntimeEvent) -> None:
        return None


def _plugin() -> Drain:
    plugin = _SilentPlugin(queue_size=8, stop_drain_timeout=1.0)

    def start() -> None:
        plugin.start()

    return start, lambda: plugin.handle_runtime_event(_event())


def test_a_producer_after_the_loop_closed_does_not_raise() -> None:
    """Start a drain, let its loop close under it, then hand it work.

    `asyncio.run` closes the loop while the drain still holds a reference to
    it, which is the state an abandoned shutdown leaves behind.
    """
    start, produce = _plugin()

    async def start_the_drain() -> None:
        start()

    asyncio.run(start_the_drain())

    produce()
