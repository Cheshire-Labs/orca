"""What a device command did, in ledger terms.

Two routes reach a driver: a workflow action, which builds the typed request
in the device bridge, and an operator's own command, which parses the same
request off the wire. Both read that request here, so one command leaves the
same records whichever route sent it.

The routes differ only in who asked and under whose identity the records are
filed. That rides on the RecordContext.

The record says what the command ASKED the instrument to do. A driver reports
its whole rack or plate back on every call, and that report is a projection
seeded from these same records, so folding it as an absolute would let a stale
projection overwrite the record it came from. ``operations_for_driver_state``
keeps it as a witness instead, marked DRIVER_OBSERVED, which nothing folds.
"""

import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import singledispatch
from typing import Callable

from cheshire_drivers.liquid_handler_models import (
    TROUGH_WELL_ID,
    Aspirate96Request,
    AspirateRequest,
    AspirateTarget,
    ChannelError,
    DiscardTipsRequest,
    Dispense96Request,
    DispenseRequest,
    DispenseTarget,
    DropTips96Request,
    DropTipsRequest,
    LabwareStateResponse,
    MixRequest,
    PickUpTips96Request,
    PickUpTipsRequest,
    ReturnTips96Request,
)
from pydantic import BaseModel, JsonValue

from orca.state.mounted import channels_for_slices
from orca.state.ops_history import OpsHistory
from orca.state.ops_store import SYSTEM_ID

logger = logging.getLogger("orca")
from orca.state.records import (
    Aspirate96Details,
    AspirateDetails,
    DeviceOperation,
    Dispense96Details,
    DispenseDetails,
    GenericOperationDetails,
    InitialStateDetails,
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


@dataclass(frozen=True)
class RecordContext:
    """Who asked for a command, and under whose identity its records are filed.

    ``default_labware`` is what a command that names no labware of its own
    touched: a discard names no rack, and a shake names nothing at all. The
    caller knows it, from the action's assigned labware or from what the ledger
    says is standing at the device.
    """

    device_name: str
    action_id: str
    thread_id: str
    source: TrackingSource
    resolve_labware_id: Callable[[str], str | None]
    default_labware: Sequence[str] = ()

    def ids_for(self, *labware: str) -> list[str]:
        """Instance ids for these names, skipping any the caller cannot resolve.

        A name with no id contributes nothing. Putting the name in the id field
        would be worse: the fold filters on a non-empty id list, so the record
        would match nothing and the labware would read as never touched.
        """
        found = (self.resolve_labware_id(name) for name in labware)
        return [instance_id for instance_id in found if instance_id is not None]


def action_context(
    device_name: str,
    affected_labware: list[str],
    affected_labware_ids: list[str],
    action_id: str,
    thread_id: str,
) -> RecordContext:
    """A workflow action's identity, and the labware it holds, as a reader wants them.

    The names and ids arrive as parallel lists; a name with no id is carried
    with no id rather than with its own name in the id field.
    """
    return RecordContext(
        device_name=device_name,
        action_id=action_id,
        thread_id=thread_id,
        source=TrackingSource.OBSERVED,
        resolve_labware_id=dict(zip(affected_labware, affected_labware_ids)).get,
        default_labware=affected_labware,
    )


RECORDED_BY_NAME: Mapping[str, str] = {
    "get_deck_state": "a read; it moves nothing",
    "get_head_configuration": "a read; it moves nothing",
    "is_connected": "a read; it moves nothing",
    "is_initialized": "a read; it moves nothing",
    "connect": "opens the link; it moves nothing",
    "disconnect": "closes the link; it moves nothing",
    "initialize": "moves the head, never labware",
    "open": "opens the instrument's door or lid, never labware",
    "close": "closes the instrument's door or lid, never labware",
    "move_plate": (
        "a move changes where labware is, not what was done to it; the ledger "
        "learns it through the location service"
    ),
    "configure_deck": (
        "the layout it installs is what the driver then reports back, and that "
        "report is kept as a witness rather than as an operation"
    ),
    "add_deck_labware": "materializes labware on the driver's deck; registration owns this",
    "remove_deck_labware": "un-materializes labware on the driver's deck; registration owns this",
    "reset_deck_labware": "pushes the ledger's answer onto the driver; it is not new evidence",
    "reconcile_deck_occupancy": "pushes the ledger's answer onto the driver; it is not new evidence",
    "reconcile_hardware_state": (
        "reads the head's tip sensor and can clear tips the driver was "
        "tracking; that changes what the head carries, which the mounted-tips "
        "ledger states and nothing writes there from this report"
    ),
    "discard_stranded_tips": (
        "throws away tips the head was found holding; same as "
        "reconcile_hardware_state, the head's own state goes unwritten"
    ),
}
"""Liquid-handler commands recorded by name alone, and why the name is enough.

A command missing from both this table and the readers below could move tips or
liquid and be saved as a record that says nothing about them. A guard test
refuses that.
"""


class UnreadCommandError(TypeError):
    """A command moved hardware and nothing here can say what it did."""


@singledispatch
def operations_for_request(
    request: BaseModel, context: RecordContext,
) -> list[OperationRecord]:
    """The operations a typed liquid-handler request performed.

    Each reader below registers against its own request type, so a reader and
    the command it reads cannot be paired wrong the way a hand-kept table can.
    A request with no reader raises: the commands recorded by name alone are
    in ``RECORDED_BY_NAME`` and never reach here.
    """
    del context
    raise UnreadCommandError(
        f"{type(request).__name__} has no reader, so the record cannot say what it did; "
        f"give it one in operations_for_request or say in RECORDED_BY_NAME why not"
    )


_no_reader = operations_for_request.dispatch(BaseModel)


def _record(
    operation: DeviceOperation,
    context: RecordContext,
    labware: Sequence[str],
    details: OperationDetails,
    timestamp: float,
) -> OperationRecord:
    return OperationRecord(
        operation=operation,
        device_name=context.device_name,
        affected_labware=list(labware),
        affected_labware_ids=context.ids_for(*labware),
        action_id=context.action_id,
        thread_id=context.thread_id,
        details=details,
        timestamp=timestamp,
        source=context.source,
    )


def _positions(positions: Sequence[str] | None, volumes: Sequence[float]) -> list[str]:
    """A single-pool container names no wells; every channel draws from its one."""
    if positions is not None:
        return list(positions)
    return [TROUGH_WELL_ID] * len(volumes)


@operations_for_request.register
def _aspirate(request: AspirateRequest, context: RecordContext) -> list[OperationRecord]:
    """One record per labware the channels reached into.

    A single call can span two plates, and folding them into one record debits
    the first for the second's liquid and leaves the second reading untouched.
    """
    now = time.time()
    return [
        _record(DeviceOperation.ASPIRATE, context, [target.labware], AspirateDetails(
            labware=target.labware,
            positions=_positions(target.positions, target.volumes),
            volumes=list(target.volumes),
        ), now)
        for target in request.aspirations
    ]


@operations_for_request.register
def _dispense(request: DispenseRequest, context: RecordContext) -> list[OperationRecord]:
    """One record per labware the channels reached into. See ``_aspirate``."""
    now = time.time()
    return [
        _record(DeviceOperation.DISPENSE, context, [target.labware], DispenseDetails(
            labware=target.labware,
            positions=_positions(target.positions, target.volumes),
            volumes=list(target.volumes),
        ), now)
        for target in request.dispenses
    ]


@operations_for_request.register
def _pick_up_tips(
    request: PickUpTipsRequest, context: RecordContext,
) -> list[OperationRecord]:
    """One record per rack the head reached into.

    Each names only its own rack: a record that debits rack A must not claim to
    have touched rack B, or an audit of one rack reads operations that never
    moved its tips.
    """
    now = time.time()
    channels, counted = channels_for_slices(
        [len(pick.positions) for pick in request.picks], _optional_list(request.use_channels),
    )
    return [
        _record(DeviceOperation.PICK_UP_TIPS, context, [pick.tip_rack], TipPickUpDetails(
            tip_rack=pick.tip_rack,
            positions=list(pick.positions),
            use_channels=engaged,
            channels_were_counted=counted,
        ), now)
        for pick, engaged in zip(request.picks, channels)
    ]


@operations_for_request.register
def _drop_tips(request: DropTipsRequest, context: RecordContext) -> list[OperationRecord]:
    """One record per rack, so tips come back to the rack they left.

    A drop to waste naming no rack empties the same channels a discard does and
    is recorded as one; there is no rack for the tips to return to.
    """
    now = time.time()
    if not request.drops:
        return [_record(
            DeviceOperation.DISCARD_TIPS, context, context.default_labware,
            TipDiscardDetails(use_channels=_optional_list(request.use_channels)), now,
        )]
    channels, _ = channels_for_slices(
        [len(drop.positions) for drop in request.drops], _optional_list(request.use_channels),
    )
    return [
        _record(DeviceOperation.DROP_TIPS, context, [drop.tip_rack], TipDropDetails(
            tip_rack=drop.tip_rack,
            positions=list(drop.positions),
            to_waste=request.to_waste,
            use_channels=engaged,
        ), now)
        for drop, engaged in zip(request.drops, channels)
    ]


@operations_for_request.register
def _discard_tips(
    request: DiscardTipsRequest, context: RecordContext,
) -> list[OperationRecord]:
    """A discard names no rack: the tips go to waste and stop existing.

    Without its own record the head keeps reporting them mounted forever, and
    refuses every pick after them.
    """
    return [_record(
        DeviceOperation.DISCARD_TIPS, context, context.default_labware,
        TipDiscardDetails(use_channels=_optional_list(request.use_channels)),
        time.time(),
    )]


@operations_for_request.register
def _mix(request: MixRequest, context: RecordContext) -> list[OperationRecord]:
    """A mix moves no net volume and no tip, so nothing folds it.

    It is still what the instrument spent that time doing, and a history that
    skips it reads as a gap between the aspirate and the dispense.
    """
    return [_record(
        DeviceOperation.MIX, context, [request.labware],
        GenericOperationDetails(command="mix", args_repr=repr(request)),
        time.time(),
    )]


@operations_for_request.register
def _aspirate96(
    request: Aspirate96Request, context: RecordContext,
) -> list[OperationRecord]:
    return [_record(
        DeviceOperation.ASPIRATE96, context, [request.labware], Aspirate96Details(
            labware=request.labware, volume=request.volume,
            flow_rate=request.flow_rate, liquid_height=request.liquid_height,
        ), time.time(),
    )]


@operations_for_request.register
def _dispense96(
    request: Dispense96Request, context: RecordContext,
) -> list[OperationRecord]:
    return [_record(
        DeviceOperation.DISPENSE96, context, [request.labware], Dispense96Details(
            labware=request.labware, volume=request.volume,
            flow_rate=request.flow_rate, liquid_height=request.liquid_height,
        ), time.time(),
    )]


@operations_for_request.register
def _pick_up_tips96(
    request: PickUpTips96Request, context: RecordContext,
) -> list[OperationRecord]:
    return [_record(
        DeviceOperation.PICK_UP_TIPS96, context, [request.tip_rack],
        TipPickUp96Details(tip_rack=request.tip_rack), time.time(),
    )]


@operations_for_request.register
def _drop_tips96(
    request: DropTips96Request, context: RecordContext,
) -> list[OperationRecord]:
    """A drop naming a rack returns the tips to it; naming none sends them to waste."""
    labware = [request.tip_rack] if request.tip_rack is not None else []
    return [_record(
        DeviceOperation.DROP_TIPS96, context, labware, TipDrop96Details(
            tip_rack=request.tip_rack, to_waste=request.to_waste,
        ), time.time(),
    )]


@operations_for_request.register
def _return_tips96(
    request: ReturnTips96Request, context: RecordContext,
) -> list[OperationRecord]:
    """The head puts its 96 tips back on the rack the driver took them from.

    Which rack that is stays with the driver: the request does not carry it and
    the response does not name it, so the record can say the return happened
    and not which rack got the tips back. Use ``drop_tips96`` with the rack
    named for a return the tip fold can follow.
    """
    del request
    return [_record(
        DeviceOperation.RETURN_TIPS96, context, context.default_labware,
        GenericOperationDetails(command="return_tips96", args_repr="()"),
        time.time(),
    )]


def _optional_list(channels: Sequence[int] | None) -> list[int] | None:
    return None if channels is None else list(channels)


def labware_named_in(operations: Sequence[OperationRecord]) -> list[str]:
    """The labware these records name, once each.

    Read from records built with no default labware, it is what the command names as its own: empty for a
    shake or a discard, the plate for an aspirate.
    """
    return list(dict.fromkeys(name for operation in operations for name in operation.affected_labware))


def reads(request_model: type[BaseModel]) -> bool:
    """Whether a typed request has a reader of its own here."""
    return operations_for_request.dispatch(request_model) is not _no_reader


def args_text(params: Mapping[str, JsonValue]) -> str:
    """A command's params as its record's `args_repr` holds them, whoever sent it."""
    return repr(dict(params))


def operations_for(
    command: str, request: BaseModel | None, args_repr: str, context: RecordContext,
) -> list[OperationRecord]:
    """The operations one device command performed, whichever route sent it."""
    if request is None or command in RECORDED_BY_NAME:
        return operations_for_command(command, args_repr, context)
    return operations_for_request(request, context)


def operations_for_command(
    command: str, args_repr: str, context: RecordContext,
) -> list[OperationRecord]:
    """The record for a command with no typed reader: a shake, a seal, a home.

    A command with no operation of its own, such as ``home``, is recorded as
    ``COMMAND``, so the history holds every command sent to a device.
    """
    try:
        operation = DeviceOperation(command)
    except ValueError:
        operation = DeviceOperation.COMMAND
    return [_record(
        operation, context, context.default_labware,
        GenericOperationDetails(command=command, args_repr=args_repr),
        time.time(),
    )]


def operations_for_completed(
    command: str,
    request: BaseModel | None,
    args_repr: str,
    response: LabwareStateResponse | None,
    context: RecordContext,
) -> tuple[list[OperationRecord], bool]:
    """The operations a completed command performed, and whether a channel of it failed.

    ``response`` is the driver's answer after the device's trust setting has
    filtered it. A partial failure is one record per channel, each saying
    whether its liquid or its tip moved; the caller decides what a failed channel means.
    """
    errors = response.per_channel_errors if response is not None else []
    if errors and isinstance(request, READ_PER_CHANNEL):
        return operations_for_channels(command, request, errors, context), True
    if errors:
        warn_unread_channel_errors(command, errors)
    return [*operations_for(command, request, args_repr, context), *operations_for_driver_state(response, context)], False


LiquidRequest = AspirateRequest | DispenseRequest
TipRequest = PickUpTipsRequest | DropTipsRequest | DiscardTipsRequest
READ_PER_CHANNEL = (AspirateRequest, DispenseRequest, PickUpTipsRequest, DropTipsRequest, DiscardTipsRequest)
"""The requests whose partial failure is saved one record per channel."""


class ChannelFailureNotReadError(RuntimeError):
    """A channel of a command failed and nothing says which channels worked, so no row can be written."""

    def __init__(self, command: str) -> None:
        super().__init__(
            f"{command!r} reported a failed channel and named no channels, so the record cannot say which "
            f"channels worked. Nothing was saved for it."
        )


def warn_unread_channel_errors(command: str, errors: Sequence[ChannelError]) -> None:
    """A command with no per-channel reader reported channel errors; say so, because they are not saved."""
    logger.warning(
        "%r reported %d per-channel error(s) and is not read per channel; they are not on the record.",
        command, len(errors),
    )


def operations_for_channels(
    command: str,
    request: LiquidRequest | TipRequest,
    errors: Sequence[ChannelError],
    context: RecordContext,
) -> list[OperationRecord]:
    """One record per channel, each naming only the labware it touched.

    A channel that errored records a zero move rather than no record: "we
    tried and it did not go" is different from silence.
    """
    if isinstance(request, (AspirateRequest, DispenseRequest)):
        return _liquid_channels(command, request, errors, context)
    return _tip_channels(command, request, errors, context)


def _liquid_channels(
    command: str, request: LiquidRequest, errors: Sequence[ChannelError], context: RecordContext,
) -> list[OperationRecord]:
    operation, details_model = (
        (DeviceOperation.ASPIRATE, AspirateDetails)
        if isinstance(request, AspirateRequest)
        else (DeviceOperation.DISPENSE, DispenseDetails)
    )
    now = time.time()
    records: list[OperationRecord] = []
    for labware, position, volume, error in _attribute_channels(command, request, errors):
        if error is not None:
            details = details_model(
                labware=labware, positions=[position], volumes=[0.0],
                certainty="definitely_not_transferred", error_code=error.error_code,
            )
        else:
            details = details_model(
                labware=labware, positions=[position], volumes=[volume],
                certainty="confirmed_transferred",
            )
        records.append(_record(operation, context, [labware], details, now))
    return records


def _tip_channels(
    command: str, request: TipRequest, errors: Sequence[ChannelError], context: RecordContext,
) -> list[OperationRecord]:
    """A failed channel's record names what it tried and moves no tip, so the rack and the head keep that tip."""
    if isinstance(request, DiscardTipsRequest):
        return _discard_channels(command, request.use_channels, errors, context)
    if isinstance(request, DropTipsRequest) and not request.drops:
        # A drop to waste naming no rack is recorded as a discard, as when it fully works.
        return _discard_channels(command, request.use_channels, errors, context)
    now = time.time()
    slices = request.picks if isinstance(request, PickUpTipsRequest) else (request.drops or [])
    channels, counted = channels_for_slices(
        [len(target.positions) for target in slices], _optional_list(request.use_channels),
    )
    # A drop to waste is reported against the waste and the channel, not against a rack spot.
    at_a_spot = [error for error in errors if error.well_position is not None]
    failed = {(error.labware, error.well_position): error for error in at_a_spot}
    failed_channel = {error.channel_id: error for error in errors if error.well_position is None}
    _warn_orphan_channel_errors(
        command, at_a_spot, {(target.tip_rack, position) for target in slices for position in target.positions},
    )
    records: list[OperationRecord] = []
    for target, engaged in zip(slices, channels):
        for position, channel in zip(target.positions, engaged):
            error = failed.get((target.tip_rack, position)) or failed_channel.get(channel)
            details: TipPickUpDetails | TipDropDetails
            if isinstance(request, PickUpTipsRequest):
                details = _one_pick(target.tip_rack, position, channel, counted, error)
                operation = DeviceOperation.PICK_UP_TIPS
            else:
                details = _one_drop(target.tip_rack, position, channel, request.to_waste, error)
                operation = DeviceOperation.DROP_TIPS
            records.append(_record(operation, context, [target.tip_rack], details, now))
    return records


def _one_pick(rack: str, position: str, channel: int, counted: bool, error: ChannelError | None) -> TipPickUpDetails:
    if error is None:
        return TipPickUpDetails(tip_rack=rack, positions=[position], use_channels=[channel], channels_were_counted=counted)
    return TipPickUpDetails(
        tip_rack=rack, positions=[], use_channels=[], channels_were_counted=counted,
        failed_positions=[position], failed_channels=[channel], error_code=error.error_code,
    )


def _one_drop(rack: str, position: str, channel: int, to_waste: bool, error: ChannelError | None) -> TipDropDetails:
    if error is None:
        return TipDropDetails(tip_rack=rack, positions=[position], to_waste=to_waste, use_channels=[channel])
    return TipDropDetails(
        tip_rack=rack, positions=[], to_waste=to_waste, use_channels=[],
        failed_positions=[position], failed_channels=[channel], error_code=error.error_code,
    )


def _discard_channels(
    command: str, use_channels: Sequence[int] | None, errors: Sequence[ChannelError], context: RecordContext,
) -> list[OperationRecord]:
    """One record per named channel. With no channels named nothing says which ones let go, so nothing is written."""
    if not use_channels:
        logger.warning(
            "%r named no channels and reported %d failed channel(s); which channels let go is unknown, so no row "
            "is written for it.", command, len(errors),
        )
        raise ChannelFailureNotReadError(command)
    failed = {error.channel_id: error for error in errors}
    named = list(use_channels)
    for channel in failed.keys() - set(named):
        logger.warning("%r: channel %d reported an error and the request did not name it.", command, channel)
    now = time.time()
    records: list[OperationRecord] = []
    for channel in named:
        error = failed.get(channel)
        details = (
            TipDiscardDetails(use_channels=[channel]) if error is None
            else TipDiscardDetails(use_channels=[], failed_channels=[channel], error_code=error.error_code)
        )
        records.append(_record(DeviceOperation.DISCARD_TIPS, context, context.default_labware, details, now))
    return records


def _attribute_channels(
    command: str,
    request: LiquidRequest,
    errors: Sequence[ChannelError],
) -> list[tuple[str, str, float, ChannelError | None]]:
    """Per-channel (labware, position, volume, error or None).

    A plate's error goes to the channel whose well it names. A trough's channels
    share one key, so its errors are attributed by count: k errors mark the
    first k of its channels not transferred. That keeps the trough's net volume
    right; attributing by position would zero every channel on one failure.
    """
    channels = _channels_in(request)
    pooled = {t.labware for t in _targets_of(request) if t.positions is None}
    pool_errors: dict[str, list[ChannelError]] = {}
    position_errors: dict[tuple[str, str], ChannelError] = {}
    for error in errors:
        if error.labware in pooled:
            pool_errors.setdefault(error.labware, []).append(error)
        else:
            position = error.well_position if error.well_position is not None else TROUGH_WELL_ID
            position_errors[(error.labware, position)] = error
    _warn_orphan_channel_errors(command, errors, {(labware, position) for labware, position, _ in channels})
    attributed: list[tuple[str, str, float, ChannelError | None]] = []
    for labware, position, volume in channels:
        if labware in pooled:
            queue = pool_errors.get(labware)
            found = queue.pop(0) if queue else None
        else:
            found = position_errors.get((labware, position))
        attributed.append((labware, position, volume, found))
    return attributed


def _targets_of(
    request: AspirateRequest | DispenseRequest,
) -> Sequence[AspirateTarget] | Sequence[DispenseTarget]:
    if isinstance(request, AspirateRequest):
        return request.aspirations
    return request.dispenses


def _channels_in(request: AspirateRequest | DispenseRequest) -> list[tuple[str, str, float]]:
    """Per-channel (labware, position, volume), in the order the wire lists them."""
    channels: list[tuple[str, str, float]] = []
    for target in _targets_of(request):
        positions = _positions(target.positions, target.volumes)
        channels.extend(
            (target.labware, position, float(volume))
            for position, volume in zip(positions, target.volumes)
        )
    return channels


def _warn_orphan_channel_errors(
    command: str, errors: Sequence[ChannelError], request_keys: set[tuple[str, str]],
) -> None:
    """A channel error on a well the request never named is a driver bug; the per-channel records miss it."""
    for error in errors:
        position = error.well_position if error.well_position is not None else TROUGH_WELL_ID
        if (error.labware, position) not in request_keys:
            logger.warning(
                "%r: channel %d reported an error on %s/%s, which the request did not name; "
                "the per-channel records miss it.",
                command, error.channel_id, error.labware, error.well_position,
            )


async def save_outside_a_run(
    history: OpsHistory, context: RecordContext, operations: list[OperationRecord],
) -> None:
    """Save a command no action asked for, in the execution `history` is bound to."""
    await history.append_record(TrackingRecord(
        execution_id=history.execution_id,
        action_id=context.action_id,
        thread_id=SYSTEM_ID,
        method_id=None,
        source=context.source,
        timestamp=time.time(),
        operations=operations,
    ))


def outside_a_run(
    device_name: str,
    command: str,
    source: TrackingSource,
    resolve_labware_id: Callable[[str], str | None] = lambda _name: None,
    default_labware: Sequence[str] = (),
) -> RecordContext:
    """The identity a command no run asked for is filed under."""
    return RecordContext(
        device_name=device_name,
        action_id=f"__{source.value}__:{device_name}:{command}:{uuid.uuid4().hex[:8]}",
        thread_id=SYSTEM_ID,
        source=source,
        resolve_labware_id=resolve_labware_id,
        default_labware=default_labware,
    )


async def save_command_by_name(
    history: OpsHistory, device_name: str, command: str, source: TrackingSource, args_repr: str,
) -> None:
    """Save a command no action asked for and that names no labware: a bring-up, an arm's reach, a deck push."""
    context = outside_a_run(device_name, command, source)
    await save_outside_a_run(history, context, operations_for_command(command, args_repr, context))


def operations_for_driver_state(
    response: LabwareStateResponse | None, context: RecordContext,
) -> list[OperationRecord]:
    """What the driver reported about its own labware when the call returned.

    A witness, not a baseline. These carry DRIVER_OBSERVED whoever asked for
    the command, because the fold refuses to take a driver's projection as
    evidence for the record that seeded it. They name the call only through the
    action id, because the snapshot covers every labware on the deck, not only
    the ones the call touched.
    """
    if response is None or not response.labware_state:
        return []
    now = time.time()
    records: list[OperationRecord] = []
    for labware_name, well_state in response.labware_state.items():
        tip_positions: list[str] | None = None
        if well_state.tips is not None:
            tip_positions = [pos for pos, present in well_state.tips.items() if present]
        records.append(OperationRecord(
            operation=DeviceOperation.INITIAL_STATE,
            device_name=context.device_name,
            affected_labware=[labware_name],
            affected_labware_ids=context.ids_for(labware_name),
            action_id=context.action_id,
            thread_id=context.thread_id,
            details=InitialStateDetails(
                labware=labware_name,
                well_volumes=(
                    dict(well_state.volumes) if well_state.volumes is not None else None
                ),
                tip_positions_present=tip_positions,
            ),
            timestamp=now,
            source=TrackingSource.DRIVER_OBSERVED,
        ))
    return records
