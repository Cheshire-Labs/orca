"""Intervention classification: which RuntimeEvents need operator attention.

A single predicate that both orca-core (plugin filters) and a hosted
deployment (the `wait_for_intervention` long-poll tool) read, so they agree on the set of
events that park a workflow waiting for a human: manual plate placement,
manual plate removal, an author-declared manual step, or a recorded
incident.
"""

from enum import Enum

from orca.events.runtime_event import RuntimeEvent
from orca.workflow_models.status_enums import LabwareThreadStatus

# Entity types / statuses that name an intervention event on the bus.
THREAD_ENTITY = "THREAD"
OPERATOR_ENTITY = "OPERATOR"
INCIDENT_ENTITY = "INCIDENT"
DEVICE_ENTITY = "DEVICE"
OPERATOR_INSTRUCTION_STATUS = "INSTRUCTION"
DEVICE_FAULTED_STATUS = "FAULTED"


class InterventionKind(str, Enum):
    """The kind of operator attention a classified event needs."""

    MANUAL_PLACE = "MANUAL_PLACE"
    MANUAL_REMOVE = "MANUAL_REMOVE"
    MANUAL_STEP = "MANUAL_STEP"
    INCIDENT = "INCIDENT"
    DEVICE_FAULT = "DEVICE_FAULT"


def classify_intervention(event: RuntimeEvent) -> InterventionKind | None:
    """Return the InterventionKind for an event, or None if it is not one.

    Kept stable across orca and a hosted deployment so the waiter and any plugin filter
    agree on what counts as needing a human.
    """
    if event.entity_type == INCIDENT_ENTITY:
        return InterventionKind.INCIDENT
    # A cleared fault is not an intervention: nothing is waiting on a person.
    if (
        event.entity_type == DEVICE_ENTITY
        and event.status == DEVICE_FAULTED_STATUS
    ):
        return InterventionKind.DEVICE_FAULT
    if (
        event.entity_type == OPERATOR_ENTITY
        and event.status == OPERATOR_INSTRUCTION_STATUS
    ):
        return InterventionKind.MANUAL_STEP
    if event.entity_type == THREAD_ENTITY:
        if event.status == LabwareThreadStatus.AWAITING_MANUAL_PLACE.value:
            return InterventionKind.MANUAL_PLACE
        if event.status == LabwareThreadStatus.AWAITING_MANUAL_REMOVE.value:
            return InterventionKind.MANUAL_REMOVE
    return None
