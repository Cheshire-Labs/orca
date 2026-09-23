"""Custom exceptions for device controller."""

import asyncio

from cheshire_drivers.driver_errors import InstrumentOutcome, outcome_of

from orca.gateway.device_fault import DeviceFault


class DeviceError(Exception):
    """Base exception for device-related errors.

    ``device_fault`` is the fault this failure left on the device, when it left
    one. The controller sets it at the moment it latches, so a caller holding
    only the exception can say which fault its pause is about instead of
    matching on a device name and hoping.
    """

    def __init__(self, *args: str) -> None:
        super().__init__(*args)
        self.device_fault: DeviceFault | None = None


class DeviceLockedError(DeviceError):
    """Device is currently locked by another operation.

    Raised when attempting to execute a command on a device that is
    already executing another command. The caller should either:
    - Try a different device (if looking for any device of type)
    - Wait and retry (if must use specific device)
    - Fail and report device busy
    """
    pass


class DeviceOfflineError(DeviceError):
    """Device is not currently connected.

    Raised when attempting to execute a command on a device that is
    not connected to the platform. The device may have disconnected
    or never connected in the first place.
    """
    pass


class DeviceUnknownError(DeviceError):
    """Device is neither declared in topology nor connected via the gateway.

    Distinct from :class:`DeviceOfflineError` (which means the device IS
    declared in topology but no connection has registered it). Callers map
    this to a not-found result; offline maps to service-unavailable.
    """
    pass


class CommandTimeoutError(DeviceError):
    """Command did not complete within timeout period.

    Raised when a command is sent to a device but no response is
    received within the specified timeout. This may indicate:
    - Device is unresponsive
    - Command is taking longer than expected
    - Network connectivity issues
    """
    pass


class InvalidCommandError(DeviceError):
    """Device type does not support this command.

    Raised when attempting to execute a command that is not supported
    by the device's type. For example, trying to send a 'shake' command
    to a centrifuge.
    """
    pass


class ConfirmationRequiredError(DeviceError):
    """A dangerous vendor command was sent without an explicit confirm.

    The driver's catalog flags every vendor extra it did not declare
    read-safe (`MethodInfo.requires_confirm`); dispatching one requires the
    caller to acknowledge it with ``confirm=True``. Raised before any wire
    send. Callers surface the refusal so a human decides, then re-send with
    the acknowledgement; nothing about the device state changes it.
    """
    pass


class DeviceFaultedError(DeviceError):
    """A previous command on this device did not come back clean.

    Raised before any wire send, on engine-dispatched commands only. An
    operator's own commands still reach the device, because looking at it and
    putting it right is what clears the fault.

    Carries the fault so a caller can tell "the driver reported a failure"
    from "no answer came back and the instrument may still be moving".
    """

    def __init__(self, message: str, fault: DeviceFault) -> None:
        super().__init__(message)
        self.fault = fault
        self.device_fault = fault


class CommandExecutionError(DeviceError):
    """Error returned from the device bridge during command execution.

    These errors originate from device drivers (e.g., PyLabRobot) and are
    safe to expose to API callers. Examples:
    - "Deprecated. Use Cor_96_wellplate_360ul_Fb instead."
    - "Resource 'tips_01' not found on deck"
    - "Cannot pick up tips: no tips at position A1"

    The error_type field contains the exception class name from the device bridge.

    ``instrument_outcome`` is what the driver said the failure left the machine
    in. None means the driver did not say, which the wire contract defines as
    failed.
    """

    def __init__(
        self,
        message: str,
        error_type: str = "CommandError",
        instrument_outcome: InstrumentOutcome | None = None,
    ):
        super().__init__(message)
        self.error_type = error_type
        self.instrument_outcome = instrument_outcome


def instrument_outcome_of(error: BaseException) -> InstrumentOutcome:
    """What this failure left the instrument in.

    Three sources, one answer. A command the device bridge answered carries
    what its driver said, and ``None`` there is the wire contract for "did not
    classify". A driver raised in this process declares it on its own class.
    Anything else never got an answer at all, so nothing has told the
    instrument to stop.

    One place, because the callers must agree: one decides whether waiting is
    worth anything and the other whether there is an instrument to fault.
    """
    if isinstance(error, CommandExecutionError):
        if error.instrument_outcome is None:
            return InstrumentOutcome.FAILED
        return error.instrument_outcome
    if isinstance(error, (DeviceError, asyncio.CancelledError)):
        return InstrumentOutcome.UNKNOWN
    return outcome_of(error)


class ModeUnresolvableError(DeviceError):
    """Which world a device is in cannot be determined right now.

    Raised when there is no live system to read the device's topology
    declaration from: the runtime is not built, or a rebuild failed and left
    it torn down. The declaration is what keeps a command off real hardware,
    so a command sent while it is unreadable is refused rather than guessed
    at. Distinct from a device the system simply does not declare, which is
    answerable. Callers map this to service-unavailable; it clears when the
    runtime builds.
    """
    pass
