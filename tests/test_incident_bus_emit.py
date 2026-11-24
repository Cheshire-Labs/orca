"""Recording an incident emits a RuntimeEvent on the SystemEventBus.

Before this, recording an incident only wrote a dict; the operating LLM
could not learn an incident had happened except by polling incidents_list.
These tests pin that every recorded incident now broadcasts an INCIDENT
RuntimeEvent (classified as an intervention) carrying the summary fields a
notifier needs, while the full typed detail stays on the incidents surface.
"""

from unittest.mock import MagicMock

from orca.events.execution_context import IncidentContext
from orca.runtime.intervention import InterventionKind, classify_intervention
from orca.runtime.incident_store import RecoverableTimeoutContext
from orca.events.runtime_event import RuntimeEvent
from orca.runtime.sinks import CollectorSink
from orca.runtime.system_runtime import SystemRuntime
from tests.memory_activity import memory_runtime


async def _runtime() -> tuple[SystemRuntime, CollectorSink]:
    system = MagicMock()
    runtime = await memory_runtime(system)
    runtime.pause_all_threads = MagicMock(  # type: ignore[method-assign]
        return_value={"pausing": 0, "already_paused": 0, "terminal_skipped": 0},
    )
    sink = CollectorSink()
    runtime.register_sink(sink)
    return runtime, sink


def _incident_events(sink: CollectorSink) -> list[RuntimeEvent]:
    return [e for e in sink.events if e.entity_type == "INCIDENT"]


async def test_recording_incident_emits_runtime_event_on_bus() -> None:
    runtime, sink = await _runtime()
    ctx = RecoverableTimeoutContext(
        device_id="shaker_1", command="shake", command_id="c1",
        elapsed_seconds=10.0, max_seconds=5.0, unheld=None,
    )

    incident = runtime.declare_recoverable_timeout(execution_id="exec-1", context=ctx)

    events = _incident_events(sink)
    assert len(events) == 1
    event = events[0]
    assert event.entity_id == incident.id
    assert event.execution_id == "exec-1"
    assert classify_intervention(event) is InterventionKind.INCIDENT


async def test_incident_event_context_carries_summary_fields() -> None:
    runtime, sink = await _runtime()
    ctx = RecoverableTimeoutContext(
        device_id="shaker_1", command="shake", command_id="c1",
        elapsed_seconds=10.0, max_seconds=5.0, unheld=None,
    )

    incident = runtime.declare_recoverable_timeout(execution_id="exec-1", context=ctx)

    event = _incident_events(sink)[0]
    assert isinstance(event.context, IncidentContext)
    assert event.context.incident_id == incident.id
    assert event.context.category == "RECOVERABLE_TIMEOUT"
    assert event.context.severity == "WARNING"
    assert event.context.recovery_action == "NONE"
    assert event.context.message == incident.message
    assert event.context.execution_id == "exec-1"


async def test_distinct_incidents_emit_distinct_events() -> None:
    runtime, sink = await _runtime()
    ctx = RecoverableTimeoutContext(
        device_id="shaker_1", command="shake", command_id="c1",
        elapsed_seconds=10.0, max_seconds=5.0, unheld=None,
    )

    a = runtime.declare_recoverable_timeout(execution_id="exec-1", context=ctx)
    b = runtime.declare_recoverable_timeout(execution_id="exec-1", context=ctx)

    ids = {e.entity_id for e in _incident_events(sink)}
    assert ids == {a.id, b.id}
