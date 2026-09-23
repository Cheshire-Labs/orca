"""Tests for GET /events (polling). SSE streaming is covered by the sink
unit tests plus a manual end-to-end check.

What these catch:
- /events endpoint is gated by _require_system_runtime (regression: if
  the gate is missing, pre-load callers would get an empty list instead
  of a clear 409).
- /events actually reads from the runtime's event history, not a stub
  list. Publishing an event makes the endpoint return it.
"""

from httpx import AsyncClient

from orca.events.execution_context import WorkflowExecutionContext
from orca.events.runtime_event import RuntimeEvent
from orca.runtime.system_runtime import SystemRuntime


async def test_events_poll_gated_on_system_loaded(
    empty_client: AsyncClient,
) -> None:
    """/events requires a loaded system; no-system-loaded must return 409."""
    resp = await empty_client.get("/events")
    assert resp.status_code == 409


async def test_events_poll_returns_published_events(
    client: AsyncClient, runtime: SystemRuntime,
) -> None:
    """With a loaded runtime, /events returns events that were published
    on its system event bus. Proves the route actually reads runtime state."""
    runtime._system_event_bus.emit(
        RuntimeEvent(
            event_name="TEST.0.RUNNING",
            execution_id="exec-test",
            timestamp=100.0,
            entity_type="TEST",
            entity_id="0",
            status="RUNNING",
            context=WorkflowExecutionContext(
                execution_id="w", workflow_name="w",
            ),
        ),
    )
    resp = await client.get("/events")
    assert resp.status_code == 200
    body = resp.json()
    assert any(e["event_name"] == "TEST.0.RUNNING" for e in body)
