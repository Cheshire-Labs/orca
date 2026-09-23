from typing import Optional

from orca.workflow_models.error_policy_overrides import OverrideWithPauseError


class DeviceError(Exception):
    """Base class for all device-related errors."""
    def __init__(self, message: str, device_name: Optional[str] = None) -> None:
        self.device_name = device_name
        self.message = message if device_name is None else f"[{device_name}] {message}"
        super().__init__(self.message)


class DeviceBusyError(DeviceError):
    """Raised when a device is already in use or staged."""
    pass


class SlotOccupiedError(DeviceBusyError):
    """A placement target already holds different labware.

    Subclasses ``DeviceBusyError`` so the engine paths that retry a busy
    target keep catching it. It carries the occupant separately from the
    message so an operator surface can name what to clear instead of only
    reporting that something is in the way.
    """

    def __init__(
        self,
        position_id: str,
        existing_labware_name: str,
        existing_template_name: str,
    ) -> None:
        self.position_id = position_id
        self.existing_labware_name = existing_labware_name
        self.existing_template_name = existing_template_name
        super().__init__(
            f"{position_id} already holds {existing_labware_name} "
            f"(template {existing_template_name})",
        )


class MoverAlreadyHoldingError(SlotOccupiedError, OverrideWithPauseError):
    """A mover was asked to pick while its jaws already hold something.

    Almost always a plate left in the jaws by an interrupted move, which the
    engine cannot clear on its own: whether it is still there is a fact only a
    human at the bench can supply. Overrides ``FailurePolicy.ABORT`` to a pause
    so the run stops for that answer instead of dying, and names the remedy,
    because the operator's next move is to say where the plate really is.
    """

    def __init__(self, mover_name: str, held_labware_name: str, held_template_name: str) -> None:
        self.mover_name = mover_name
        super().__init__(
            position_id=f"{mover_name}/gripper",
            existing_labware_name=held_labware_name,
            existing_template_name=held_template_name,
        )
        self.args = (
            f"{mover_name} is already holding {held_labware_name} "
            f"(template {held_template_name}), so it cannot pick. If the jaws are "
            f"really empty, or the plate is somewhere else, say so with "
            f"`orca labware edit-location {held_labware_name} <position>`.",
        )


class CommandTimeoutAbortedError(DeviceError):
    """Raised when an operator aborts a recoverable-timeout-held device command.

    The command exceeded its advertised ``max_seconds`` and the operator chose
    ABORT over extend/mark-complete. Propagates as a normal action failure so
    the workflow's failure policy fires.
    """
    pass


class DeviceNotInitializedError(DeviceError):
    """Raised when an operation is attempted on a device that hasn't been initialized."""
    pass


class DeviceUnderExternalControlError(DeviceError, OverrideWithPauseError):
    """Raised when an action tries to dispatch against a device that is
    currently under external (gateway / operator) control.

    A hosted device-integration gateway is the higher-priority surface by
    design: it is the troubleshooting path operators (or AI agents) use
    when Orca is stuck or hardware needs hands-on attention. Orca
    workflows back off while gateway control is held so production
    samples don't race against an operator's recovery commands.

    Inherits ``OverrideWithPauseError`` so the action-error handler routes
    to PAUSE regardless of the method's declared ``FailurePolicy``. An
    ``ABORT`` declaration expresses what to do when *the method itself*
    fails; an external hold on the device is not a method failure, so
    the operator (not the workflow author) decides recovery.

    Audit / "why" lives in operations history + the pause UI; this error
    only carries the device name so the operator-facing envelope can
    point at the right resource.
    """

    def __init__(self, device_name: str) -> None:
        super().__init__(
            "device is under external (gateway) control",
            device_name=device_name,
        )

class DeviceStateDivergenceError(DeviceError, OverrideWithPauseError):
    """Raised when a hardware-state reconcile finds a divergence no automatic
    repair can resolve (e.g. a physically seated tip the driver does not track).

    Inherits ``OverrideWithPauseError`` for the same reason external control
    does: the divergence is not a failure of the method being retried, so the
    operator (not the workflow author's FailurePolicy) decides recovery --
    typically ``discard_stranded_tips`` or hands-on resolution at the
    instrument, then resume.
    """
    pass


class TipsNotPresentError(DeviceError):
    """Raised when a pick's requested positions read empty in the ledger,
    checked before the device call runs.

    On hardware with no tip-detection sensor a pick against an empty
    position would otherwise just fail into thin air with nothing to catch
    it -- this reports the same way a driver's own "no tip detected" failure
    already does, through the normal operation-recovery seam, so the
    operator's fix is identical either way: load tips, correct the ledger
    with ``orca labware set-tips``, then RETRY_OP. Deliberately NOT an
    ``OverrideWithPauseError``: that marker skips the retry seam entirely
    (caught and re-raised before the recovery handler ever sees it), which
    would make RETRY_OP unavailable here.
    """

    def __init__(
        self, rack_name: str, rack_id: str, missing_positions: list[str], remaining_count: int,
    ) -> None:
        self.rack_name = rack_name
        self.rack_id = rack_id
        self.missing_positions = missing_positions
        self.remaining_count = remaining_count
        positions = ", ".join(missing_positions)
        tip_word = "tip" if remaining_count == 1 else "tips"
        super().__init__(
            f"{rack_name}: positions {positions} read empty in the ledger "
            f"({remaining_count} {tip_word} remain). Load tips and correct it "
            f"with `orca labware set-tips {rack_id} --position ... "
            f"--reason ...`, then retry."
        )
