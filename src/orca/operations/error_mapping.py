"""The Operation errors a route answers with for what an Operation can raise."""

from orca.gateway.controller.command_pipeline import PartialChannelFailureError
from orca.gateway.controller.exceptions import DeviceError
from orca.operations.device_commands import (
    device_error, device_error_code, external_control_error,
)
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.resource_models.labware_placement import PositionNotSavedError
from orca.resource_models.device_error import (
    DeviceUnderExternalControlError, TeachpointNotFoundError, TipsNotPresentError,
)


def known_operation_error(exc: Exception) -> OperationError | None:
    """The OperationError for `exc`, or None when nothing maps it."""
    if isinstance(exc, OperationError):
        return exc
    if isinstance(exc, DeviceError):
        return device_error(exc)
    if isinstance(exc, DeviceUnderExternalControlError):
        return external_control_error(exc)
    if isinstance(exc, PositionNotSavedError):
        return OperationError.of(ErrorCode.POSITION_NOT_SAVED, str(exc), labware_id=exc.labware_id)
    return pipeline_error(exc)


def teachpoint_not_found(exc: TeachpointNotFoundError) -> OperationError:
    return OperationError.of(
        ErrorCode.TEACHPOINT_NOT_FOUND, str(exc),
        available_teachpoints=list(exc.available_teachpoints),
    )


def pipeline_error(exc: Exception) -> OperationError | None:
    """A refusal or a failed channel the command pipeline raises for any sender."""
    if isinstance(exc, TeachpointNotFoundError):
        return teachpoint_not_found(exc)
    if isinstance(exc, TipsNotPresentError):
        return OperationError.of(
            ErrorCode.TIPS_NOT_PRESENT, str(exc),
            labware_id=exc.rack_id, positions=list(exc.missing_positions),
        )
    if isinstance(exc, PartialChannelFailureError):
        return OperationError.of(
            ErrorCode.CHANNEL_FAILED,
            f"{exc} The channels that worked are saved; check the record." if exc.records
            else f"{exc} Look at the device and state what it holds.",
        )
    return None


def error_label(exc: BaseException) -> str:
    """`code: message` under the code the operator surface answers with, for every command's events."""
    if isinstance(exc, DeviceError):
        return f"{device_error_code(exc)}: {exc}"
    if isinstance(exc, Exception):
        known = pipeline_error(exc)
        if known is not None:
            return f"{known.surface_code()}: {exc}"
    return f"unknown: {exc}"
