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

from pydantic import JsonValue


@dataclass(frozen=True)
class RemedyStep:
    """One call in a remedy, on whichever surface the caller is using."""

    verb: str
    label: str
    args: dict[str, JsonValue]
    mcp_tool: str
    rest: str
    cli: str
    needs: tuple[str, ...] = field(default_factory=tuple)
    """Argument names the operator still has to supply.

    A remedy that cannot name the plate in the way is still worth offering:
    it says what to do and leaves the one blank a person has to fill. Silently
    dropping the step instead would hide the only useful half.
    """
    query: tuple[str, ...] = field(default_factory=tuple)
    """Argument names the REST route takes in the query string, not the body.

    A client that posts every argument as JSON gets a 400 from a route that
    wanted one in the URL, so the step has to say which.
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
    labware_id: str | None = None
    labware_name: str | None = None
    device_name: str | None = None
    paused_device_command: str | None = None


def _recover(decision: str, ctx: RemedyContext, label: str) -> RemedyStep:
    return RemedyStep(
        verb=f"thread.recover.{decision}",
        label=label,
        args={
            "execution_id": ctx.execution_id,
            "thread_id": ctx.thread_id,
            "decision": decision,
        },
        mcp_tool="operations_recover_thread",
        rest="POST /api/operations/recover-thread",
        cli=(
            f"orca thread recover {ctx.execution_id} {ctx.thread_id} "
            f"--decision {decision}"
        ),
    )


def _edit_location(ctx: RemedyContext) -> RemedyStep:
    return RemedyStep(
        verb="labware.edit_location",
        label="Say where the plate actually is",
        args={"labware_id": ctx.labware_id},
        mcp_tool="operations_edit_labware_location",
        rest="POST /api/operations/edit-labware-location",
        cli=f"orca labware edit-location {ctx.labware_id or '<labware_id>'} <location>",
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
                mcp_tool="operations_discharge_labware",
                rest="POST /api/operations/discharge-labware",
                cli="orca labware discharge <labware_id>",
                needs=("labware_id",),
            ),
            _recover("RETRY", ctx, "Retry the step"),
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
                mcp_tool="reservation_cancel",
                rest=(
                    f"DELETE /api/executions/{ctx.execution_id}"
                    "/reservations/<reservation_id>"
                ),
                cli=f"orca reservation cancel {ctx.execution_id} <reservation_id>",
                needs=("reservation_id", "reason"),
                query=("reason",),
            ),
            _recover("RETRY", ctx, "Retry the step"),
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
                mcp_tool="operations_mark_tips_used",
                rest="POST /api/operations/mark-tips-used",
                cli=f"orca labware mark-tips-used {ctx.labware_id or '<labware_id>'}",
                needs=("positions",),
            ),
            _recover("RETRY_OP", ctx, "Retry just the failed call"),
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
                mcp_tool="operations_set_well_volumes",
                rest="POST /api/operations/set-well-volumes",
                cli=f"orca labware set-volume {ctx.labware_id or '<labware_id>'}",
                needs=("volumes",),
            ),
            _recover("RETRY_OP", ctx, "Retry just the failed call"),
        ),
    )


def _head_disagrees_about_tips(ctx: RemedyContext) -> Remedy:
    device = ctx.device_name or "<device>"
    return Remedy(
        id="reconcile_the_head_then_retry",
        label="Reconcile the head, then retry the action",
        explain=(
            "The driver and the instrument disagree about what is on the "
            "channels. Reconcile rebuilds a dead session and clears tips that "
            "are definitely gone. The op-level retry is refused after a "
            "divergence, so the whole action is the retry here."
        ),
        recommended=True,
        steps=(
            RemedyStep(
                verb="liquid_handler.reconcile_hardware_state",
                label="Reconcile the head",
                args={"device_id": ctx.device_name},
                mcp_tool="liquid_handler-reconcile_hardware_state",
                rest=f"POST /api/liquid-handlers/{device}/reconcile_hardware_state",
                cli=f"orca device send {device} reconcile_hardware_state",
            ),
            _recover("RETRY", ctx, "Retry the action"),
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
    """The named recoveries for this failure, best first. Empty when none fit."""
    found: list[Remedy] = []
    if error_type is not None:
        build = _BY_ERROR_TYPE.get(error_type)
        if build is not None:
            found.append(build(ctx))
    if at_move:
        found.extend(build(ctx) for build in _MOVE_PAUSE_REMEDIES)
    return tuple(found)


def known_error_types() -> frozenset[str]:
    """Every exception class the catalogue recognises. Read by the guard test."""
    return frozenset(_BY_ERROR_TYPE)
