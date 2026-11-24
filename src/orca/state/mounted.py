"""What is mounted on each channel of a liquid handler head.

The driver holds a belief about this and refuses work when it disagrees with
reality, which is the wrong way round: the belief is one orca projected onto it.
Folding the record answers the same question from the side that can be checked.
Nothing sends the record to a driver; Orca reads it to decide whether a vendor
protocol may run.

A channel is identified by its index on a named device. Nothing here resolves a
device or a rack to an object.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from orca.state.confirm_refusals import (
    NothingToConfirmError, WorkLostError,
)
from orca.state.ops_history import OpsHistory
from orca.state.ops_store import ops_for_device
from orca.state.provenance import Provenance
from orca.state.unsettled import set_verb
from orca.state.records import (
    GAPS_THAT_LOST_WORK,
    DeviceOperation,
    HeadObservationGapDetails,
    ObservationGapCause,
    MountedTipsAssertedDetails,
    MountedTipsConfirmedDetails,
    OperationRecord,
    TipDiscardDetails,
    TipDrop96Details,
    TipDropDetails,
    TipPickUp96Details,
    TipPickUpDetails,
)


class MalformedTipRecord(ValueError):
    """A record whose channels and positions cannot be paired."""


class Head(Enum):
    """Which of a liquid handler's heads a tip is on. Each head numbers its own channels from zero."""

    CHANNELS = "channels"
    HEAD96 = "head96"


@dataclass(frozen=True)
class MountedTip:
    """The tip on one channel, named by where it came from."""

    tip_rack: str
    position: str
    channel_is_inferred: bool
    """The operation that put this tip on named no channel, so the channel it
    is filed under was counted from zero rather than observed."""
    head: Head = Head.CHANNELS


@dataclass(frozen=True)
class MountedTips:
    """What a head is carrying, and how well the record knows it."""

    by_channel: dict[int, MountedTip]
    """The channel head: the independent pipetting channels."""
    provenance: Provenance
    gaps: frozenset[ObservationGapCause] = frozenset()
    """Why the record stopped being sure, when gaps are what stopped it. Empty
    when the read is stale for another reason, or not stale at all."""
    head96: dict[int, MountedTip] = field(default_factory=dict)
    """The 96 head, by its own channel numbers 0 to 95. Empty on a handler that has none."""

    def on(self, channel: int) -> MountedTip | None:
        return self.by_channel.get(channel)


def channels_for_slices(
    widths: list[int], use_channels: list[int] | None,
) -> tuple[list[list[int]], bool]:
    """Which channels each slice of one command engaged, and whether it counted.

    One call slices across racks and writes a record per rack, but the channels
    engage in target order across the whole call, so the first slice takes the
    first ``widths[0]``. A slice that counted from zero on its own would claim
    channels the slice before it already holds, and the fold would drop those
    tips. A caller who named no channels, or named a count that does not match
    the positions, gets them counted across the whole call and marked.
    """
    total = sum(widths)
    counted = use_channels is None or len(use_channels) != total
    engaged = list(range(total)) if counted else list(use_channels or [])
    per_slice: list[list[int]] = []
    at = 0
    for width in widths:
        per_slice.append(engaged[at:at + width])
        at += width
    return per_slice, counted


def _channels_for(positions: list[str], use_channels: list[int] | None) -> list[int]:
    """Channels in the same order as the positions they acted on.

    A record naming none is read as a straight fill from channel zero, which is
    how a head fills and is what the wire leaves us to infer.
    """
    if use_channels is None:
        return list(range(len(positions)))
    if len(use_channels) != len(positions):
        raise MalformedTipRecord(
            f"a tip record names {len(use_channels)} channels for "
            f"{len(positions)} positions; zipping them would silently drop tips"
        )
    return list(use_channels)


_TIP_COMMANDS_OF_THE_96_HEAD = frozenset({"pick_up_tips96", "drop_tips96", "return_tips96"})
_TIP_COMMANDS_OF_THE_CHANNELS = frozenset({"pick_up_tips", "drop_tips", "discard_tips"})


def head_a_failed_command_leaves_unknown(command: str) -> Head | None:
    """The head whose tips a failed command leaves unknown: only a tip command can have taken tips on or off."""
    if command in _TIP_COMMANDS_OF_THE_96_HEAD:
        return Head.HEAD96
    if command in _TIP_COMMANDS_OF_THE_CHANNELS:
        return Head.CHANNELS
    return None


HEAD96_POSITIONS = tuple(f"{row}{column}" for column in range(1, 13) for row in "ABCDEFGH")
"""The rack spot each 96-head channel takes. The head fills column by column: channel 0 takes A1, channel 8
takes A2 and channel 95 takes H12."""

# What a tip operation cannot speak for: it names the channels it used, and a
# cut-off command may have touched any of the others.
_SURVIVES_A_TIP_OPERATION = frozenset({ObservationGapCause.OPERATIONS_DROPPED})


def fold_mounted(ops: list[OperationRecord], device_name: str) -> MountedTips:
    """What the record says this device's head is carrying.

    An observation gap does not move a tip, but it does mean nobody was
    watching, so the answer needs a look again. Gaps are written on a restart
    and on a reconnect, the two events that rebuild the driver's own beliefs;
    an error pause marks the labware a thread is carrying and not the head,
    because a paused thread need not be at a liquid handler at all. A tip
    operation after a gap re-establishes the record: the head is back to being
    watched, and saying otherwise leaves a head that dropped everything asking
    to be looked at over a state it now knows with certainty. The gap a cut-off
    command leaves is the exception: a pick speaks only about the channels it
    used, and that command may have touched any of the others. Only an
    operator saying what is mounted, or a discard that takes everything off,
    settles that one.

    A handler can carry two heads, and each is folded on its own. A channel
    command speaks only for the channel head and a 96-head command only for the
    96 head, in what they carry and in which doubt they settle. A gap row from a
    cut-off tip command says which head it drives; a restart or a reconnect is
    about both. A gap about both puts the 96 head in doubt only once the record has
    heard of that head: most handlers have none, and a head nothing ever
    mentioned cannot keep a device stale. A cut-off 96-head command is itself
    the record hearing of it.
    """
    carried: dict[int, MountedTip] = {}
    on_head96: dict[int, MountedTip] = {}
    seen_any = False
    seen_head96 = False
    gaps: set[ObservationGapCause] = set()
    gaps96: set[ObservationGapCause] = set()

    for op in ops:
        if op.device_name != device_name:
            continue
        details = op.details
        if isinstance(details, TipPickUpDetails):
            seen_any = True
            gaps &= _SURVIVES_A_TIP_OPERATION
            inferred = details.use_channels is None or details.channels_were_counted
            channels = _channels_for(details.positions, details.use_channels)
            for channel, position in zip(channels, details.positions):
                carried[channel] = MountedTip(
                    details.tip_rack, position, channel_is_inferred=inferred,
                )
        elif isinstance(details, TipDropDetails):
            seen_any = True
            gaps &= _SURVIVES_A_TIP_OPERATION
            for channel in _channels_for(details.positions, details.use_channels):
                carried.pop(channel, None)
        elif isinstance(details, TipPickUp96Details):
            seen_any = seen_head96 = True
            gaps96 &= _SURVIVES_A_TIP_OPERATION
            on_head96 = {
                channel: MountedTip(details.tip_rack, position, channel_is_inferred=False, head=Head.HEAD96)
                for channel, position in enumerate(HEAD96_POSITIONS)
            }
        elif isinstance(details, TipDrop96Details) or op.operation is DeviceOperation.RETURN_TIPS96:
            # A 96-head drop takes every tip off that head, so it settles even a cut-off command's doubt about it.
            seen_any = seen_head96 = True
            gaps96.clear()
            on_head96 = {}
        elif isinstance(details, TipDiscardDetails):
            seen_any = True
            if details.use_channels is None:
                gaps.clear()
                carried.clear()
            else:
                gaps &= _SURVIVES_A_TIP_OPERATION
                for channel in details.use_channels:
                    carried.pop(channel, None)
        elif isinstance(details, MountedTipsAssertedDetails):
            seen_any = True
            gaps.clear()
            gaps96.clear()
            carried = {
                channel: MountedTip(rack, position, channel_is_inferred=False)
                for channel, (rack, position) in details.by_channel.items()
            }
            on_head96 = {
                channel: MountedTip(rack, position, channel_is_inferred=False, head=Head.HEAD96)
                for channel, (rack, position) in details.head96.items()
            }
            seen_head96 = seen_head96 or bool(on_head96)
        elif isinstance(details, MountedTipsConfirmedDetails):
            seen_any = True
            gaps.clear()
            gaps96.clear()
        elif isinstance(details, HeadObservationGapDetails):
            if details.head != Head.HEAD96.value:
                gaps.add(details.cause)
            if details.head != Head.CHANNELS.value:
                gaps96.add(details.cause)
            # A cut-off 96-head command is the record hearing of the 96 head: it may now hold tips.
            seen_head96 = seen_head96 or details.head == Head.HEAD96.value

    if not seen_any:
        return MountedTips({}, Provenance.UNKNOWN)
    standing = frozenset(gaps | gaps96) if seen_head96 else frozenset(gaps)
    if not standing:
        return MountedTips(carried, Provenance.KNOWN, head96=on_head96)
    return MountedTips(carried, Provenance.STALE, standing, head96=on_head96)


async def name_the_rack_a_96_return_went_to(
    operations: list[OperationRecord], ops_history: OpsHistory, labware_id_of: Callable[[str], str | None],
) -> list[OperationRecord]:
    """A `return_tips96` names no rack: the head puts each tip back in the spot it took it from.

    Read those spots off the record of the 96 head: one row per rack, crediting exactly the spots the head holds
    from it. A return from a 96 head the record says is empty stays as the plain row, which credits nothing.
    """
    named: list[OperationRecord] = []
    for operation in operations:
        if operation.operation is not DeviceOperation.RETURN_TIPS96:
            named.append(operation)
            continue
        ops = await ops_for_device(ops_history.store, operation.device_name)
        spots_by_rack: dict[str, list[str]] = {}
        for tip in fold_mounted(ops, operation.device_name).head96.values():
            spots_by_rack.setdefault(tip.tip_rack, []).append(tip.position)
        if not spots_by_rack:
            named.append(operation)
            continue
        for rack, spots in spots_by_rack.items():
            rack_id = labware_id_of(rack)
            named.append(operation.model_copy(update={
                "details": TipDrop96Details(tip_rack=rack, to_waste=False, positions=spots),
                "affected_labware": [rack],
                "affected_labware_ids": [] if rack_id is None else [rack_id],
            }))
    return named


class MountedTipsLedger:
    """What each head is carrying, owned by the record rather than the driver."""

    def __init__(self, ops_history: OpsHistory) -> None:
        self._ops_history = ops_history

    async def of(self, device_name: str) -> MountedTips:
        """What the record says about this head."""
        ops = await ops_for_device(self._ops_history.store, device_name)
        return fold_mounted(ops, device_name)

    async def assert_mounted(
        self, device_name: str, by_channel: dict[int, tuple[str, str]],
        head96: dict[int, tuple[str, str]] | None = None,
    ) -> None:
        """An operator says this is what is on each head, channel by (rack,
        position). Absolute: an empty map says that head is carrying nothing."""
        await self._ops_history.append_set_mounted_tips(device_name, by_channel, head96 or {})

    async def note_observation_gap(
        self, device_name: str, cause: ObservationGapCause,
        head: Literal["channels", "head96"] | None = None, execution_id: str | None = None,
    ) -> None:
        """Nobody was watching this head for a while. Moves no tip; expires an
        attestation."""
        history = self._ops_history if execution_id is None else self._ops_history.for_execution(execution_id)
        await history.append_head_observation_gap(device_name, cause, head)

    async def confirm(self, device_name: str) -> None:
        """The operator agrees with what the record already says.

        Writes its own record, so agreement is a statement and not merely the
        absence of a correction. It restates no tips: the operator agreed the
        head carries these, and was never shown which channel numbers the
        record had guessed, so guessed ones stay marked.
        """
        mounted = await self.of(device_name)
        if mounted.provenance is Provenance.UNKNOWN:
            raise NothingToConfirmError(
                f"nothing has ever said what {device_name!r} is carrying, so "
                f"there is nothing to confirm; state it with set_mounted_tips",
                settle_with=set_verb("mounted-tips"),
            )
        if mounted.gaps & GAPS_THAT_LOST_WORK:
            raise WorkLostError(
                f"a stop, a timeout or a failure cut off a command that may have moved tips on "
                f"{device_name!r}, so agreeing with this read would write down a "
                f"head that may be carrying more than it says; look at the head "
                f"and state it with set_mounted_tips",
                settle_with=set_verb("mounted-tips"),
            )
        await self._ops_history.append_confirm_mounted_tips(device_name)
