"""The orca Operation each remedy action runs, where orca owns that action."""

from orca.operations.binding import BoundOperation
from orca.operations.device import (
    ClearDeviceFaultOperation,
    CompareDeckOperation,
    ReleaseDeviceControlOperation,
)
from orca.operations.labware import (
    DischargeLabwareOperation,
    EditLabwareLocationOperation,
    MarkTipsUsedOperation,
    RegisterLabwareOperation,
    SetWellVolumesOperation,
)
from orca.operations.manual_step import ConfirmManualStepOperation
from orca.operations.runtime_reads import CancelReservationOperation
from orca.operations.thread import RecoverThreadOperation, ResumeOperation
from orca.operations.typed_device_commands import typed_operation
from orca.runtime.remedies import RemedyAction

REMEDY_OPERATIONS: dict[RemedyAction, type[BoundOperation]] = {
    RemedyAction.RECOVER_THREAD: RecoverThreadOperation,
    RemedyAction.RESUME: ResumeOperation,
    RemedyAction.EDIT_LABWARE_LOCATION: EditLabwareLocationOperation,
    RemedyAction.REGISTER_LABWARE: RegisterLabwareOperation,
    RemedyAction.DISCHARGE_LABWARE: DischargeLabwareOperation,
    RemedyAction.MARK_TIPS_USED: MarkTipsUsedOperation,
    RemedyAction.SET_WELL_VOLUMES: SetWellVolumesOperation,
    RemedyAction.CLEAR_DEVICE_FAULT: ClearDeviceFaultOperation,
    RemedyAction.COMPARE_DECK: CompareDeckOperation,
    RemedyAction.RELEASE_DEVICE_CONTROL: ReleaseDeviceControlOperation,
    RemedyAction.CONFIRM_MANUAL_STEP: ConfirmManualStepOperation,
    RemedyAction.CANCEL_RESERVATION: CancelReservationOperation,
    RemedyAction.RECONCILE_HARDWARE_STATE: typed_operation("/liquid-handlers/{device_id}/reconcile-hardware-state"),
}
