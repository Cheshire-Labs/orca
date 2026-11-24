"""Named recoveries for the failures that keep happening.

A recovery verb assumes the operator already knows the system. Most recoveries
are several steps in an order that matters, and the same handful of situations
recur on the bench: a plate in another plate's way, a tip column run out, a
gripper that closed on nothing. A remedy is one of those situations written
down once -- what it is, what to do, in what order.

Matching keys on the exception CLASS NAME, never on parsing a message. The
class name arrives on the incident detail (``error_type``) and on a device
fault record, both of which the engine stamps rather than formats. A message
is prose and changes; a class name is a contract.

A shape with no entry here falls back to the bare recovery verbs, which is
what every pause offered before this module existed. Adding a situation is one
entry, so the catalogue is where bench experience accumulates.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from pydantic import JsonValue

from orca.workflow_models.status_enums import RecoveryDecision


class RemedyAction(str, Enum):
    """The calls orca-core knows. A server routes those it serves and adds its own as strings."""

    RECOVER_THREAD = "recover_thread"
    RESUME = "resume"
    EDIT_LABWARE_LOCATION = "edit_labware_location"
    REGISTER_LABWARE = "register_labware"
    DISCHARGE_LABWARE = "discharge_labware"
    MARK_TIPS_USED = "mark_tips_used"
    SET_WELL_VOLUMES = "set_well_volumes"
    CANCEL_RESERVATION = "cancel_reservation"
    CLEAR_DEVICE_FAULT = "clear_device_fault"
    COMPARE_DECK = "compare_deck"
    RELEASE_DEVICE_CONTROL = "release_device_control"
    CONFIRM_MANUAL_STEP = "confirm_manual_step"
    RECONCILE_HARDWARE_STATE = "reconcile_hardware_state"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class RemedyStep:
    """One call in a remedy. The surface that sends it says where the call goes."""

    verb: str
    label: str
    args: dict[str, JsonValue]
    action: str
    cli: str
    needs: tuple[str, ...] = field(default_factory=tuple)
    """Argument names the operator still has to supply.

    A remedy that cannot name the plate in the way is still worth offering:
    it says what to do and leaves the one blank a person has to fill. Silently
    dropping the step instead would hide the only useful half.
    """


@dataclass(frozen=True)
class Remedy:
    """A named recovery: what it does, and the steps in order."""

    id: str
    label: str
    explain: str
    steps: tuple[RemedyStep, ...]
    recommended: bool = False
    confirm: bool = False
    """Whether this needs an explicit go. Set when it moves hardware, discards
    work, or overrides something another thread is relying on."""


@dataclass(frozen=True)
class RemedyContext:
    """What the catalogue gets to work with when building a remedy."""

    execution_id: str
    thread_id: str
    honoured_decisions: frozenset[str]
    """The decisions the pause accepts. A remedy is built only from these."""
    labware_id: str | None = None
    labware_name: str | None = None
    device_name: str | None = None


def _recover(decision: str, ctx: RemedyContext, label: str) -> RemedyStep:
    return RemedyStep(
        verb=f"thread.recover.{decision}",
        label=label,
        args={
            "execution_id": ctx.execution_id,
            "thread_id": ctx.thread_id,
            "decision": decision,
        },
        action=RemedyAction.RECOVER_THREAD,
        cli=(
            f"orca execution thread recover {ctx.execution_id} {ctx.thread_id} "
            f"{RecoveryDecision(decision).cli_word}"
        ),
    )


def _retry_where_it_stopped(ctx: RemedyContext) -> RemedyStep:
    """The retry this pause takes: the failed step where one failed, else the action whose own code failed."""
    if "RETRY" in ctx.honoured_decisions:
        return _recover("RETRY", ctx, "Retry the failed step")
    return _recover("RETRY_ACTION", ctx, "Retry the action")


def _edit_location(ctx: RemedyContext) -> RemedyStep:
    return RemedyStep(
        verb="labware.edit_location",
        label="Say where the plate actually is",
        args={"labware_id": ctx.labware_id},
        action=RemedyAction.EDIT_LABWARE_LOCATION,
        cli=f"orca labware edit-location {ctx.labware_id or '<labware_id>'} <location> --reason <reason>",
        needs=("location", "reason"),
    )


def _plate_in_the_way(ctx: RemedyContext) -> Remedy:
    return Remedy(
        id="clear_the_plate_in_the_way",
        label="Take the plate that is in the way off, then retry",
        explain=(
            "The target already holds a plate. Discharge that occupant, then "
            "re-run the step. Nothing is discarded: the occupant leaves the "
            "live registry and its record stays queryable."
        ),
        recommended=True,
        confirm=True,
        steps=(
            RemedyStep(
                verb="labware.discharge",
                label="Take the occupant off",
                args={},
                action=RemedyAction.DISCHARGE_LABWARE,
                cli="orca labware discharge <labware_id>",
                needs=("labware_id",),
            ),
            _retry_where_it_stopped(ctx),
        ),
    )


def _position_already_reserved(ctx: RemedyContext) -> Remedy:
    return Remedy(
        id="release_the_hold_then_retry",
        label="Release the hold on that position, then retry",
        explain=(
            "Another thread holds the position this one needs. Cancelling that "
            "reservation is an operator override: if the holder still needs the "
            "position it has to reserve it again."
        ),
        recommended=True,
        confirm=True,
        steps=(
            RemedyStep(
                verb="reservation.cancel",
                label="Cancel the hold",
                args={"execution_id": ctx.execution_id},
                action=RemedyAction.CANCEL_RESERVATION,
                cli=f"orca reservation cancel <reservation_id> --execution {ctx.execution_id} --reason <reason>",
                needs=("reservation_id", "reason"),
            ),
            _retry_where_it_stopped(ctx),
        ),
    )


def _tip_column_empty(ctx: RemedyContext) -> Remedy:
    return Remedy(
        id="advance_past_the_empty_tips",
        label="Move on to the next tips, then retry the call",
        explain=(
            "The rack has no tip where the head reached. Mark what it took as "
            "used so the next resolve picks the following column, then re-run "
            "just the failed call -- the work the action already did stays done."
        ),
        recommended=True,
        steps=(
            RemedyStep(
                verb="labware.mark_tips_used",
                label="Mark the empty positions used",
                args={"labware_id": ctx.labware_id},
                action=RemedyAction.MARK_TIPS_USED,
                cli=(
                    f"orca labware mark-tips-used {ctx.labware_id or '<labware_id>'} "
                    "--position <positions> --reason <reason>"
                ),
                needs=("positions", "reason"),
            ),
            _recover("RETRY", ctx, "Retry just the failed call"),
        ),
    )


def _not_enough_liquid(ctx: RemedyContext) -> Remedy:
    return Remedy(
        id="record_the_real_volume",
        label="Record what is actually in the well, then retry the call",
        explain=(
            "The ledger thinks the well holds less than the aspirate wants. "
            "Set the real volume, then re-run just the failed call. Setting a "
            "volume is authoritative and overwrites the wells it names."
        ),
        recommended=True,
        steps=(
            RemedyStep(
                verb="labware.set_well_volumes",
                label="Set the real volume",
                args={"labware_id": ctx.labware_id},
                action=RemedyAction.SET_WELL_VOLUMES,
                cli=(
                    f"orca labware set-volume {ctx.labware_id or '<labware_id>'} "
                    "--well <well_volumes> --reason <reason>"
                ),
                needs=("well_volumes", "reason"),
            ),
            _recover("RETRY", ctx, "Retry just the failed call"),
        ),
    )


def _head_disagrees_about_tips(ctx: RemedyContext) -> Remedy:
    device = ctx.device_name or "<device>"
    return Remedy(
        id="reconcile_the_head_then_retry",
        label="Reconcile the head, then retry",
        explain=(
            "The driver and the instrument disagree about what is on the "
            "channels. Reconcile rebuilds a dead session and clears tips that "
            "are definitely gone. RETRY then repeats the failed call. Where the "
            "action's own code failed, RETRY_ACTION re-runs the action instead."
        ),
        recommended=True,
        steps=(
            RemedyStep(
                verb="liquid_handler.reconcile_hardware_state",
                label="Reconcile the head",
                args={} if ctx.device_name is None else {"device_id": ctx.device_name},
                action=RemedyAction.RECONCILE_HARDWARE_STATE,
                cli=f"orca device send {device} reconcile_hardware_state",
                needs=("device_id",) if ctx.device_name is None else (),
            ),
            _retry_where_it_stopped(ctx),
        ),
    )


def _gripper_closed_on_nothing(ctx: RemedyContext) -> Remedy:
    return Remedy(
        id="find_the_plate_then_retry_the_move",
        label="Say where the plate is, then retry the move",
        explain=(
            "The jaws closed on nothing, so the plate is not where the move "
            "was planned from. Record where it actually is and the thread "
            "throws the old plan away and routes from there."
        ),
        recommended=True,
        steps=(_edit_location(ctx), _recover("RETRY", ctx, "Retry the move")),
    )


def _plate_not_at_the_source(ctx: RemedyContext) -> Remedy:
    return Remedy(
        id="correct_the_source_then_retry",
        label="Say where the plate is, then retry the move",
        explain=(
            "The move was planned from where the plate used to be. Record "
            "where it is now and the route is planned again from there -- "
            "anywhere an arm can reach will do, including the original source."
        ),
        recommended=True,
        steps=(_edit_location(ctx), _recover("RETRY", ctx, "Retry the move")),
    )


def _finish_the_move_by_hand(ctx: RemedyContext) -> Remedy:
    return Remedy(
        id="finish_the_move_by_hand",
        label="I carried the plate to the target myself",
        explain=(
            "Records where the plate is, then tells the thread the move is "
            "done. Only accepted once the ledger puts the plate at the move's "
            "TARGET; anywhere else, correct the position and retry instead."
        ),
        confirm=True,
        steps=(_edit_location(ctx), _recover("CONTINUE", ctx, "The move is done")),
    )


_BY_ERROR_TYPE: dict[str, Callable[[RemedyContext], Remedy]] = {
    "SlotOccupiedError": _plate_in_the_way,
    "LocationReservedError": _position_already_reserved,
    "NoTipError": _tip_column_empty,
    "HasTipError": _head_disagrees_about_tips,
    "DeviceStateDivergenceError": _head_disagrees_about_tips,
    "TooLittleLiquidError": _not_enough_liquid,
    "TooLittleVolumeError": _not_enough_liquid,
    "EmptyGripError": _gripper_closed_on_nothing,
    "LabwareNotAtSourceError": _plate_not_at_the_source,
}

# Offered at every move pause, whatever raised: an operator who has already
# carried the plate needs this whether or not the failure has a named remedy.
_MOVE_PAUSE_REMEDIES: tuple[Callable[[RemedyContext], Remedy], ...] = (
    _finish_the_move_by_hand,
)


def remedies_for(
    error_type: str | None, ctx: RemedyContext, *, at_move: bool = False,
) -> tuple[Remedy, ...]:
    """The named recoveries for this failure, best first. Empty when none fit, and none the pause would refuse."""
    found: list[Remedy] = []
    if error_type is not None:
        build = _BY_ERROR_TYPE.get(error_type)
        if build is not None:
            found.append(build(ctx))
    if at_move:
        found.extend(build(ctx) for build in _MOVE_PAUSE_REMEDIES)
    return tuple(remedy for remedy in found if _the_pause_takes_it(remedy, ctx))


def _the_pause_takes_it(remedy: Remedy, ctx: RemedyContext) -> bool:
    return all(
        step.args["decision"] in ctx.honoured_decisions
        for step in remedy.steps if step.action is RemedyAction.RECOVER_THREAD
    )


def known_error_types() -> frozenset[str]:
    """Every exception class the catalogue recognises. Read by the guard test."""
    return frozenset(_BY_ERROR_TYPE)
