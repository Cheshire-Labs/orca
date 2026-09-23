"""Residency: which labware will not exit its device (drain-predicate input)."""
from orca.system.thread_registry_interface import IThreadRegistry
from orca.workflow_models.actions.location_action import ResidencyCheck
from orca.workflow_models.status_enums import LabwareThreadStatus
from orca.workflow_models.status_manager import StatusManager


def build_residency_check(
    thread_registry: IThreadRegistry, status_manager: StatusManager
) -> ResidencyCheck:
    """Labware that stays put and must not hold a rule-7 deferred release open:
    reused-reagent / immovable residents, labware whose thread ends by leaving it
    where it is, and join-waiting stayers holding in place for the next action
    (e.g. a BATCHABLE receiver plate).

    A LEAVE_IN_PLACE end is a declaration that the labware is never coming off
    that site. Leaving it out means the deferred release waits on a departure
    that will never happen, and the device stays reserved for the life of the
    runtime: the next execution needing it waits forever with nothing logged.
    """

    def is_resident(labware_id: str) -> bool:
        try:
            thread = thread_registry.get_thread_by_labware(labware_id)
        except KeyError:
            return False
        template = thread.thread_template
        if template is not None and (
            template.start_reuse_existing
            or template.immovable
            or template.end_leave_in_place
        ):
            return True
        try:
            status = status_manager.get_status(thread.id)
        except KeyError:
            return False
        return status == LabwareThreadStatus.AWAITING_CO_THREADS.name

    return is_resident
