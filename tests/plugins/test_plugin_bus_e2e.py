"""End-to-end: a real SystemEventBus emit reaches the webhook.

The other plugin tests call handle_runtime_event directly. This one wires the
plugin to a real orca-core SystemEventBus the way register_plugin does
(bus.subscribe(plugin.handle_runtime_event)), emits a RuntimeEvent through
the real bus, and asserts the webhook POST fires off the emit path. It is the
test that catches a regression in the bus->listener->offload->deliver chain
or in the RuntimeEvent shape.
"""

import time
from collections.abc import Awaitable, Callable

import httpx
import pytest
from orca.events.execution_context import WorkflowExecutionContext
from orca.events.runtime_event import RuntimeEvent
from orca.runtime.system_event_bus import SystemEventBus

from orca.plugins.webhook import WebhookPlugin


pytestmark = pytest.mark.asyncio


async def test_emit_through_real_bus_reaches_webhook(
    wait_until: Callable[..., Awaitable[None]],
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    plugin = WebhookPlugin(
        url="https://lims.example/hook",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    plugin.start()

    bus = SystemEventBus()
    bus.subscribe(plugin.handle_runtime_event)

    event = RuntimeEvent(
        event_name="INCIDENT.i-1.RAISED",
        execution_id="exec-1",
        timestamp=time.time(),
        entity_type="INCIDENT",
        entity_id="i-1",
        status="RAISED",
        context=WorkflowExecutionContext(
            execution_id="exec-1", workflow_name="wf",
        ),
    )

    # Synchronous emit returns without delivering: the POST happens on the drain.
    bus.emit(event)
    assert captured == []

    await wait_until(lambda: len(captured) == 1)
    await plugin.stop()

    assert len(captured) == 1
    assert str(captured[0].url) == "https://lims.example/hook"
    assert captured[0].read().decode().replace(" ", "").count('"entity_id":"i-1"') == 1
