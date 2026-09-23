"""Intervention classification: the shared predicate orca and a hosted deployment agree on.

A workflow can park waiting for a human in four ways (manual plate place,
manual plate remove, author-declared manual step, recorded incident). These
tests pin which RuntimeEvents `classify_intervention` flags and, critically,
that ordinary status events (RUNNING, COMPLETED, a manual pause) are NOT
flagged so the long-poll waiter never wakes the operator for nothing.
"""

from orca.events.execution_context import (
    IncidentContext,
    ManualInterventionContext,
    OperatorInstructionContext,
    ThreadExecutionContext,
)
from orca.events.intervention import (
    InterventionKind,
    classify_intervention,
)
from orca.events.runtime_event import RuntimeEvent
from orca.workflow_models.status_enums import LabwareThreadStatus


def _thread_event(status: str, context: ThreadExecutionContext) -> RuntimeEvent:
    return RuntimeEvent(
        event_name=f"THREAD.t1.{status}",
        execution_id="exec-1",
        timestamp=0.0,
        entity_type="THREAD",
        entity_id="t1",
        status=status,
        context=context,
    )


def _manual_ctx() -> ManualInterventionContext:
    return ManualInterventionContext(
        execution_id="exec-1", workflow_name="wf",
        thread_id="t1", thread_name="tn", template_name="tt",
        labware_id="lw-1", labware_name="plate_1", target_location="hotel_A1",
    )


def test_manual_place_event_classifies_as_manual_place() -> None:
    event = _thread_event(
        LabwareThreadStatus.AWAITING_MANUAL_PLACE.value, _manual_ctx(),
    )
    assert classify_intervention(event) is InterventionKind.MANUAL_PLACE


def test_manual_remove_event_classifies_as_manual_remove() -> None:
    event = _thread_event(
        LabwareThreadStatus.AWAITING_MANUAL_REMOVE.value, _manual_ctx(),
    )
    assert classify_intervention(event) is InterventionKind.MANUAL_REMOVE


def test_operator_instruction_event_classifies_as_manual_step() -> None:
    ctx = OperatorInstructionContext(
        execution_id="exec-1", workflow_name="wf",
        thread_id="t1", instruction="Load reagents into slot A1", step_id="manual_step-ab12",
    )
    event = RuntimeEvent(
        event_name="OPERATOR.INSTRUCTION",
        execution_id="exec-1",
        timestamp=0.0,
        entity_type="OPERATOR",
        entity_id="",
        status="INSTRUCTION",
        context=ctx,
    )
    assert classify_intervention(event) is InterventionKind.MANUAL_STEP


def test_incident_event_classifies_as_incident() -> None:
    ctx = IncidentContext(
        incident_id="inc-1", category="ACTION_FAILED", severity="ERROR",
        message="dispense failed", recovery_action="THREAD_RECOVER_RETRY",
        execution_id="exec-1", thread_id="t1",
    )
    event = RuntimeEvent(
        event_name="INCIDENT.inc-1.ACTION_FAILED",
        execution_id="exec-1",
        timestamp=0.0,
        entity_type="INCIDENT",
        entity_id="inc-1",
        status="ACTION_FAILED",
        context=ctx,
    )
    assert classify_intervention(event) is InterventionKind.INCIDENT


def test_ordinary_status_events_are_not_interventions() -> None:
    base = ThreadExecutionContext(
        execution_id="exec-1", workflow_name="wf",
        thread_id="t1", thread_name="tn", template_name="tt",
    )
    for status in (
        LabwareThreadStatus.MOVING.value,
        LabwareThreadStatus.COMPLETED.value,
        LabwareThreadStatus.PAUSED.value,
        LabwareThreadStatus.EXECUTING_ACTION.value,
    ):
        event = _thread_event(status, base)
        assert classify_intervention(event) is None
