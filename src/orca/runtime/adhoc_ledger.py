"""What an operator's own device command leaves behind in the ledger.

A workflow action records everything it does: the operation interpreter turns
the call into ledger records, and a move writes the location service. An ad-hoc
command reaches the same driver and changes the same hardware, so it has to
leave the same trail.

Not leaving one is worse than a gap. The ledger is authoritative AND it is
pushed back down: the next deck reconcile rebuilds the driver's deck from it.
So an operator who fixes a deck by hand and then initializes the device has
their own work written back out, and the one model that was right becomes
wrong too.

What gets recorded is the OPERATION, not the driver's answer. A driver reports
its whole rack or plate on every call, and that report is a projection seeded
from this same ledger, so folding it back as an absolute would let a stale
projection overwrite the record it came from. A pick-up recorded as "these
spots, off this rack" folds the same way the workflow's does.

Recording never fails the command. The hardware has already moved by the time
this runs, so raising here would report a failure that did not happen; a
recording that cannot be made is logged loudly instead.

A recording the ledger REFUSES is a different thing, and the log is not where
it belongs. When the position the record would claim already holds different
labware, two records now contradict each other and only a person can settle
it, so that goes to the incident surface.
"""

import logging
import time
import uuid
from collections.abc import Mapping, Sequence

from cheshire_drivers.lh_request_validation import LH_REQUEST_MODELS
from cheshire_drivers.liquid_handler_models import (
    TROUGH_WELL_ID,
    Aspirate96Request,
    AspirateRequest,
    DiscardTipsRequest,
    Dispense96Request,
    DispenseRequest,
    DropTips96Request,
    DropTipsRequest,
    MovePlateRequest,
    PickUpTips96Request,
    PickUpTipsRequest,
)
from pydantic import BaseModel, JsonValue, ValidationError

from orca.resource_models.device_error import SlotOccupiedError
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.transporter_base import TransporterBase
from orca.state.mounted import channels_for_slices
from orca.state.records import (
    ObservationGapCause,
    Aspirate96Details,
    AspirateDetails,
    DeviceOperation,
    Dispense96Details,
    DispenseDetails,
    OperationDetails,
    OperationRecord,
    TipDiscardDetails,
    TipDrop96Details,
    TipDropDetails,
    TipPickUp96Details,
    TipPickUpDetails,
    TrackingRecord,
    TrackingSource,
)
from orca.state.ops_store import SYSTEM_ID
from orca.system.system_interface import (
    DeckConflictReason,
    DeckReconcileConflict,
    ISystem,
    LedgerContradiction,
)

logger = logging.getLogger("orca")

# The arm's own pick and place. Both name a teachpoint, never a labware: what
# was picked is whatever the ledger had at that position.
_ARM_PICK_COMMANDS = frozenset({"pick_at_coords"})
_ARM_PLACE_COMMANDS = frozenset({"place_at_coords"})


async def record_operator_command(
    system: ISystem,
    device_name: str,
    command: str,
    params: Mapping[str, JsonValue],
) -> None:
    """Write what an ad-hoc command did to the ledger. Never raises."""
    try:
        if command in _ARM_PICK_COMMANDS or command in _ARM_PLACE_COMMANDS:
            await _follow_arm(system, device_name, command, params)
            return
        request = _parse_request(command, params)
        if request is None:
            return
        if isinstance(request, MovePlateRequest):
            await _follow_move(system, device_name, request)
            return
        await _record_operations(system, device_name, command, request)
    except Exception:
        logger.warning(
            "the ledger did not learn about the ad-hoc %r on %r; its answers "
            "about that labware are now the state from BEFORE the command",
            command, device_name, exc_info=True,
        )


def _parse_request(
    command: str, params: Mapping[str, JsonValue],
) -> BaseModel | None:
    """The typed wire request for a liquid-handler command, or None.

    None covers everything this does not track: a command with no request model
    (a non-LH device), and a payload the model rejects -- which the driver would
    have rejected too, so there is nothing to record either way.
    """
    entry = LH_REQUEST_MODELS.get(command)
    if entry is None:
        return None
    _, model = entry
    try:
        return model.model_validate(dict(params))
    except ValidationError:
        return None


# -- Moves -------------------------------------------------------------------


async def _follow_move(
    system: ISystem, device_name: str, request: MovePlateRequest,
) -> None:
    """Put the labware where the operator's move actually put it."""
    instance = _labware_named(system, request.plate)
    if instance is None:
        # A plate the driver knows and the ledger does not: nothing to move.
        return
    target = _deck_location(system, device_name, request.to_position)
    if target is None:
        return
    await _move_in_ledger(
        system, device_name, instance, target,
        _present_location(system, instance),
    )


async def _move_in_ledger(
    system: ISystem, device_name: str, instance: LabwareInstance,
    target: Location, source: Location | None,
) -> None:
    """Record a move that already happened.

    One record holds the position, so a relocation cannot leave the labware
    standing at both ends of it. A busy target is refused with the source
    intact.

    A labware with no present position has no source to move it off: it is
    registered as expected somewhere and this command is what puts it down.
    Handing the relocation a source of None raised inside the recorder, which
    swallows what it cannot record, so the move reached the deck and the ledger
    kept the position from before it.
    """
    if source is target:
        return
    try:
        if source is None:
            await system.labware_placer.place(instance, target)
        else:
            await system.labware_placer.relocate(instance, source, target)
    except SlotOccupiedError as occupied:
        # The claim is the placer's first step, so a refusal here means
        # nothing was written and the source still holds the record.
        _report_target_occupied(system, device_name, instance, target, occupied)
        return


def _report_target_occupied(
    system: ISystem, device_name: str, instance: LabwareInstance,
    target: Location, occupied: SlotOccupiedError,
) -> None:
    """Tell the operator that the record could not be written, and why.

    Not a recording that failed to go through: two records that cannot both
    be true, which only a person can settle. The log the swallow writes is
    not a surface anyone reads, so this goes on the incident list.
    """
    system.notify_deck_reconcile_conflict(DeckReconcileConflict(
        device_name=device_name,
        labware_id=instance.id,
        labware_name=instance.name,
        position_id=target.position_id,
        reason=DeckConflictReason.LEDGER_TARGET_OCCUPIED,
        blocking_labware_name=occupied.existing_labware_name,
    ))


async def _follow_arm(
    system: ISystem, device_name: str, command: str,
    params: Mapping[str, JsonValue],
) -> None:
    """Follow an operator's own pick or place with the arm.

    Neither command names a labware: a pick takes whatever is at the teachpoint
    and a place puts down whatever is in the jaws, so the ledger's own answer to
    those two questions is what moves. When the ledger has nothing there, the
    operator was jogging or teaching and there is nothing to record.
    """
    mover = _mover_named(system, device_name)
    site = _teachpoint_location(system, params)
    if mover is None or site is None:
        return
    if command in _ARM_PICK_COMMANDS:
        labware = site.labware
        if labware is not None:
            await _move_in_ledger(
                system, device_name, labware, mover.gripper_location, site,
            )
        return
    labware = mover.labware
    if labware is not None:
        await _move_in_ledger(
            system, device_name, labware, site, mover.gripper_location,
        )


def _mover_named(system: ISystem, device_name: str) -> TransporterBase | None:
    try:
        resource = system.get_resource(device_name)
    except KeyError:
        return None
    return resource if isinstance(resource, TransporterBase) else None


def _teachpoint_location(
    system: ISystem, params: Mapping[str, JsonValue],
) -> Location | None:
    """The Location an arm teachpoint picks from or places onto.

    Teachpoints are routinely named after a device, and a device name is the
    reservation mutex rather than a site. The mutex holds no labware, so a
    pick there reads empty and records nothing, and a place there parks the
    plate on the mutex, where the occupancy check then refuses every action on
    that device for the life of the process. Only the placement resolver turns
    a device name into the site it stands for.
    """
    teachpoint = params.get("teachpoint")
    if not isinstance(teachpoint, dict):
        return None
    position_id = teachpoint.get("position_id")
    if not isinstance(position_id, str):
        return None
    try:
        return system.system_map.resolve_placement_location(position_id)
    except KeyError:
        return None


def _deck_location(
    system: ISystem, device_name: str, site: str,
) -> Location | None:
    """The engine Location behind a driver deck site.

    Drivers name sites bare (``carrier-7-0``, ``B2-slot``); the engine addresses
    them under the device's mutex (``lh/carrier-7-0``). A site the layout does
    not provide resolves to nothing rather than a guess. Always site-qualified,
    so unlike a teachpoint this can never come back as the bare mutex.
    """
    system_map = system.system_map
    device_location = system_map.get_resource_location(device_name)
    prefix = device_location.owner_mutex_id or device_location.position_id
    try:
        return system_map.get_location(f"{prefix}/{site}")
    except KeyError:
        return None


# -- Liquid handling ---------------------------------------------------------


async def _record_operations(
    system: ISystem, device_name: str, command: str, request: BaseModel,
) -> None:
    entries = _details_for(request)
    if not entries:
        return
    # Read BEFORE the append: the question is what the record held when the
    # operator acted, and the record of the command itself would answer it.
    contradictions = await _contradictions_in(system, device_name, command, entries)
    action_id = f"__adhoc__:{device_name}:{command}:{uuid.uuid4().hex[:8]}"
    now = time.time()
    # OPERATOR, not OBSERVED: the fold reads the operation and ignores the
    # source, so this is purely how an auditor tells a hand-sent command from
    # the engine's own.
    operations = [
        OperationRecord(
            operation=operation,
            device_name=device_name,
            affected_labware=[labware] if labware is not None else [],
            affected_labware_ids=(
                _ids_for(system, labware) if labware is not None else []
            ),
            action_id=action_id,
            thread_id=SYSTEM_ID,
            details=details,
            timestamp=now,
            source=TrackingSource.OPERATOR,
        )
        for operation, labware, details in entries
    ]
    await system.ops_history.append_record(TrackingRecord(
        execution_id=SYSTEM_ID,
        action_id=action_id,
        thread_id=SYSTEM_ID,
        method_id=None,
        source=TrackingSource.OPERATOR,
        timestamp=now,
        operations=operations,
    ))
    await _report_contradictions(system, contradictions)


async def _contradictions_in(
    system: ISystem, device_name: str, command: str,
    entries: list[tuple[DeviceOperation, str | None, OperationDetails]],
) -> list[tuple[LedgerContradiction, LabwareInstance]]:
    """The entries that only make sense if the record was wrong.

    Tips only, for now. A pick is the one operation whose precondition the
    record can state exactly: it says which spots hold a tip. Volumes cannot
    do this -- a plate nobody has described reads unknown, and an aspirate
    against unknown is not a disagreement, it is the ordinary case.
    """
    found: list[tuple[LedgerContradiction, LabwareInstance]] = []
    for _operation, labware_name, details in entries:
        if not isinstance(details, (TipPickUpDetails, TipPickUp96Details)):
            continue
        if labware_name is None:
            continue
        rack = _labware_named(system, labware_name)
        if rack is None:
            continue
        # A 96-head draws from every spot at once and names none of them, so
        # the rack's own layout is what the pick claimed.
        wanted = (
            details.positions if isinstance(details, TipPickUpDetails)
            else rack.every_tip_position()
        )
        # The same question the workflow's pre-flight asks, asked of the same
        # object: an undescribed rack backs nothing and contradicts nothing.
        missing = await rack.missing_tip_positions(wanted)
        if not missing:
            continue
        if await rack.went_unobserved():
            # Nobody has looked since the last observation gap, so the fold
            # describes then. A pick finding a tip there is what a stale
            # reading looks like when it is right, not a disagreement.
            continue
        held = await rack.tip_count_present()
        found.append((LedgerContradiction(
            device_name=device_name,
            command=command,
            labware_id=rack.id,
            labware_name=labware_name,
            positions=list(missing),
            believed=(
                f"the record had {held} tips on it and none at "
                f"{', '.join(missing)}"
            ),
        ), rack))
    return found


async def _report_contradictions(
    system: ISystem,
    contradictions: list[tuple[LedgerContradiction, LabwareInstance]],
) -> None:
    """Tell the operator, and stop the fold reading as an answer.

    Both halves are needed and neither is enough. The incident reaches someone
    without their having to ask; the observation gap is what makes the labware
    say, at every read afterwards, that a person still owes it a layout. The
    command itself cannot supply one: it proved there was a tip where the
    record said none, and said nothing about the rest of the rack.
    """
    for contradiction, instance in contradictions:
        await system.labware_contents.note_observation_gap(
            instance.ref, ObservationGapCause.OPERATOR_CONTRADICTED,
        )
        system.notify_ledger_contradiction(contradiction)


def _details_for(
    request: BaseModel,
) -> list[tuple[DeviceOperation, str | None, OperationDetails]]:
    """One entry per labware the command touched, or one naming none.

    A single wire call can slice across racks or plates, so it can produce more
    than one record; the fold keys on labware, so a slice that lost its own
    record would leave that labware untracked.
    """
    if isinstance(request, PickUpTipsRequest):
        channels, counted = channels_for_slices(
            [len(pick.positions) for pick in request.picks], request.use_channels,
        )
        return [
            (DeviceOperation.PICK_UP_TIPS, pick.tip_rack, TipPickUpDetails(
                tip_rack=pick.tip_rack, positions=list(pick.positions),
                use_channels=engaged, channels_were_counted=counted,
            ))
            for pick, engaged in zip(request.picks, channels)
        ]
    if isinstance(request, DropTipsRequest):
        if not request.drops:
            # `drops` is mandatory when returning, so no drops means waste:
            # no rack is debited and the head is the only thing that changes.
            return [(DeviceOperation.DROP_TIPS, None, TipDiscardDetails(
                use_channels=(
                    list(request.use_channels)
                    if request.use_channels is not None else None
                ),
            ))]
        channels, _ = channels_for_slices(
            [len(drop.positions) for drop in request.drops], request.use_channels,
        )
        return [
            (DeviceOperation.DROP_TIPS, drop.tip_rack, TipDropDetails(
                tip_rack=drop.tip_rack, positions=list(drop.positions),
                to_waste=request.to_waste, use_channels=engaged,
            ))
            for drop, engaged in zip(request.drops, channels)
        ]
    if isinstance(request, AspirateRequest):
        return [
            (DeviceOperation.ASPIRATE, target.labware, AspirateDetails(
                labware=target.labware,
                positions=_positions(target.positions, target.volumes),
                volumes=list(target.volumes),
            ))
            for target in request.aspirations
        ]
    if isinstance(request, DispenseRequest):
        return [
            (DeviceOperation.DISPENSE, target.labware, DispenseDetails(
                labware=target.labware,
                positions=_positions(target.positions, target.volumes),
                volumes=list(target.volumes),
            ))
            for target in request.dispenses
        ]
    if isinstance(request, DiscardTipsRequest):
        # A discard names no rack: the tips go to waste and stop existing, so
        # nothing debits a labware and the head is the only thing that changes.
        return [(DeviceOperation.DISCARD_TIPS, None, TipDiscardDetails(
            use_channels=(
                list(request.use_channels)
                if request.use_channels is not None else None
            ),
        ))]
    # No entry for mix on purpose. It moves no net volume and no tip, nothing
    # folds it, and the workflow path records only an untyped marker -- writing
    # a typed one here would put two shapes for one operation in the same log.
    if isinstance(request, Aspirate96Request):
        return [(DeviceOperation.ASPIRATE96, request.labware, Aspirate96Details(
            labware=request.labware, volume=request.volume,
            flow_rate=request.flow_rate, liquid_height=request.liquid_height,
        ))]
    if isinstance(request, Dispense96Request):
        return [(DeviceOperation.DISPENSE96, request.labware, Dispense96Details(
            labware=request.labware, volume=request.volume,
            flow_rate=request.flow_rate, liquid_height=request.liquid_height,
        ))]
    if isinstance(request, PickUpTips96Request):
        return [(DeviceOperation.PICK_UP_TIPS96, request.tip_rack,
                 TipPickUp96Details(tip_rack=request.tip_rack))]
    if isinstance(request, DropTips96Request):
        if request.tip_rack is None:
            return []
        return [(DeviceOperation.DROP_TIPS96, request.tip_rack, TipDrop96Details(
            tip_rack=request.tip_rack, to_waste=request.to_waste,
        ))]
    return []


def _positions(
    positions: Sequence[str] | None, volumes: Sequence[float],
) -> list[str]:
    """A single-pool container names no wells; every channel draws from its one."""
    if positions is not None:
        return list(positions)
    return [TROUGH_WELL_ID] * len(volumes)


# -- Ledger plumbing ---------------------------------------------------------


def _labware_named(system: ISystem, name: str) -> LabwareInstance | None:
    try:
        return system.get_labware(name)
    except KeyError:
        return None


def _present_location(system: ISystem, instance: LabwareInstance) -> Location | None:
    try:
        return system.labware_location_service.get(instance)
    except KeyError:
        return None


def _ids_for(system: ISystem, labware_name: str) -> list[str]:
    instance = _labware_named(system, labware_name)
    return [instance.id] if instance is not None else []


