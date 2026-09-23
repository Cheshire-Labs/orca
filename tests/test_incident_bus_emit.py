"""Recording an incident emits a RuntimeEvent on the SystemEventBus.

Before this, recording an incident only wrote a dict; the operating LLM
could not learn an incident had happened except by polling incidents_list.
These tests pin that every recorded incident now broadcasts an INCIDENT
RuntimeEvent (classified as an intervention) carrying the summary fields a
notifier needs, while the full typed detail stays on the incidents surface.
"""

from unittest.mock import MagicMock

from orca.events.execution_context import IncidentContext
from orca.events.intervention import InterventionKind, classify_intervention
from orca.runtime.incident_store import RecoverableTimeoutContext
from orca.runtime.system_runtime import SystemRuntime


def _runtime() -> SystemRuntime:
    system = MagicMock()
    runtime = SystemRuntime(system)
    runtime.pause_all_threads = MagicMock(  # type: ignore[method-assign]
        return_value={"pausing": 0, "already_paused": 0, "terminal_skipped": 0},
    )
    return runtime


def _incident_events(runtime: SystemRuntime) -> list:
    return [e for e in runtime.get_events_since(None) if e.entity_type == "INCIDENT"]


def test_recording_incident_emits_runtime_event_on_bus() -> None:
    runtime = _runtime()
    ctx = RecoverableTimeoutContext(
        device_id="shaker_1", command="shake", command_id="c1",
        elapsed_seconds=10.0, max_seconds=5.0,
    )

    incident = runtime.declare_recoverable_timeout(execution_id="exec-1", context=ctx)

    events = _incident_events(runtime)
    assert len(events) == 1
    event = events[0]
    assert event.entity_id == incident.id
    assert event.execution_id == "exec-1"
    assert classify_intervention(event) is InterventionKind.INCIDENT


def test_incident_event_context_carries_summary_fields() -> None:
    runtime = _runtime()
    ctx = RecoverableTimeoutContext(
        device_id="shaker_1", command="shake", command_id="c1",
        elapsed_seconds=10.0, max_seconds=5.0,
    )

    incident = runtime.declare_recoverable_timeout(execution_id="exec-1", context=ctx)

    event = _incident_events(runtime)[0]
    assert isinstance(event.context, IncidentContext)
    assert event.context.incident_id == incident.id
    assert event.context.category == "RECOVERABLE_TIMEOUT"
    assert event.context.severity == "WARNING"
    assert event.context.recovery_action == "NONE"
    assert event.context.message == incident.message
    assert event.context.execution_id == "exec-1"


def test_distinct_incidents_emit_distinct_events() -> None:
    runtime = _runtime()
    ctx = RecoverableTimeoutContext(
        device_id="shaker_1", command="shake", command_id="c1",
        elapsed_seconds=10.0, max_seconds=5.0,
    )

    a = runtime.declare_recoverable_timeout(execution_id="exec-1", context=ctx)
    b = runtime.declare_recoverable_timeout(execution_id="exec-1", context=ctx)

    ids = {e.entity_id for e in _incident_events(runtime)}
    assert ids == {a.id, b.id}
