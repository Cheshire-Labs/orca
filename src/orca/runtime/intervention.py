"""Intervention classification: which events need operator attention.

The next-intervention Operation reads this one predicate. The events it names park
a workflow waiting for a human: manual plate placement, manual plate removal, an
author-declared manual step, a recorded incident, or a device fault.
"""

from enum import Enum
from typing import Protocol

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


class ClassifiableEvent(Protocol):
    """A live `RuntimeEvent` or a stored one: classification reads only these two fields."""

    @property
    def entity_type(self) -> str: ...

    @property
    def status(self) -> str: ...


def classify_intervention(event: ClassifiableEvent) -> InterventionKind | None:
    """Return the InterventionKind for an event, or None if it is not one."""
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
