"""What a device is left in when one of its commands does not come back clean.

A command that fails at the driver, times out, or is cancelled can leave the
instrument mid-motion: a gripper closed on a plate, an axis part-way to a
target. The command reports its own failure to its own caller and nothing else
changes, so the very next command dispatches against a machine nobody has
looked at. On the bench that was a gantry traversing with a plate in its jaws.

A fault is that missing state. It is latched on the device, it survives until
an operator clears it, and while it stands the engine cannot drive the device.
"""

import time
from dataclasses import dataclass
from enum import Enum


FAULTED_STATUS = "faulted"
"""What a device's status reads while a fault stands on it.

The device bridge keeps reporting the driver as ready, because the driver is
idle. The engine still cannot drive the device, so an operator surface that
showed the device bridge's word would say "ready" about a machine nothing may
touch."""


class DeviceFaultOutcome(Enum):
    """Whether we know what the machine did.

    The difference decides what an operator has to check, so it is recorded
    rather than inferred from the error text.
    """

    FAILED = "failed"
    """The driver ran the command and reported it failed. The instrument is
    stopped, somewhere between where it started and where it was going."""

    UNKNOWN = "unknown"
    """The command left and no answer came back: it timed out, it was
    cancelled, or the device dropped and stayed away. Nothing tells the
    instrument to stop, so it may still be moving."""


@dataclass(frozen=True)
class DeviceFault:
    """The command that faulted a device, kept until an operator clears it."""

    device_id: str
    command: str
    command_id: str
    outcome: DeviceFaultOutcome
    error: str
    error_type: str
    at: float
    execution_id: str | None = None

    @property
    def may_still_be_moving(self) -> bool:
        """True when nothing confirmed the instrument stopped."""
        return self.outcome is DeviceFaultOutcome.UNKNOWN

    def describe(self) -> str:
        """One sentence an operator can act on."""
        if self.may_still_be_moving:
            state = (
                "No answer came back, so the instrument may still be moving and "
                "nothing has told it to stop"
            )
        else:
            state = (
                "The driver reported it failed, so the instrument is stopped "
                "part-way through it"
            )
        return (
            f"{self.device_id}: {self.command!r} did not come back clean. "
            f"{state}. Look at the machine and put it right. A clean initialize "
            f"or home clears this by itself; otherwise clear the fault to let "
            f"the workflow drive it again. ({self.error_type}: {self.error})"
        )


def fault_from_error(
    *,
    device_id: str,
    command: str,
    command_id: str,
    error: BaseException,
    outcome: DeviceFaultOutcome,
    execution_id: str | None,
) -> DeviceFault:
    """Build the record from the error that ended the command."""
    return DeviceFault(
        device_id=device_id,
        command=command,
        command_id=command_id,
        outcome=outcome,
        error=str(error) or type(error).__name__,
        error_type=getattr(error, "error_type", None) or type(error).__name__,
        at=time.time(),
        execution_id=execution_id,
    )


@dataclass(frozen=True)
class DeviceFaultSummary:
    """A device left part-way through a command, as an operator read sees it.

    ``outcome`` is ``failed`` when the driver reported the failure itself, and
    ``unknown`` when no answer came back at all. The difference decides what to
    check: a failed command stopped, an unknown one may still be running.
    """
    command: str
    outcome: str
    error: str
    error_type: str
    at: float
    may_still_be_moving: bool
    message: str
    execution_id: str | None = None

    @classmethod
    def of(cls, fault: DeviceFault) -> "DeviceFaultSummary":
        """The read shape of this fault. One conversion, so no surface drifts."""
        return cls(
            command=fault.command,
            outcome=fault.outcome.value,
            error=fault.error,
            error_type=fault.error_type,
            at=fault.at,
            may_still_be_moving=fault.may_still_be_moving,
            message=fault.describe(),
            execution_id=fault.execution_id,
        )
