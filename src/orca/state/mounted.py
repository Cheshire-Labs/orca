"""What is mounted on each channel of a liquid handler head.

The driver holds a belief about this and refuses work when it disagrees with
reality, which is the wrong way round: the belief is one orca projected onto it.
Folding the record answers the same question from the side that can be checked,
and it is what lets a head be seeded with something true after a restart.

A channel is identified by its index on a named device. Nothing here resolves a
device or a rack to an object.
"""

from dataclasses import dataclass

from orca.state.ops_history import OpsHistory
from orca.state.ops_store import ops_for_device
from orca.state.provenance import Provenance
from orca.state.records import (
    GAPS_THAT_LOST_WORK,
    HeadObservationGapDetails,
    ObservationGapCause,
    MountedTipsAssertedDetails,
    MountedTipsConfirmedDetails,
    OperationRecord,
    TipDiscardDetails,
    TipDropDetails,
    TipPickUpDetails,
)


class MalformedTipRecord(ValueError):
    """A record whose channels and positions cannot be paired."""


@dataclass(frozen=True)
class MountedTip:
    """The tip on one channel, named by where it came from."""

    tip_rack: str
    position: str
    channel_is_inferred: bool
    """The operation that put this tip on named no channel, so the channel it
    is filed under was counted from zero rather than observed."""


@dataclass(frozen=True)
class MountedTips:
    """What a head is carrying, and how well the record knows it."""

    by_channel: dict[int, MountedTip]
    provenance: Provenance
    gaps: frozenset[ObservationGapCause] = frozenset()
    """Why the record stopped being sure, when gaps are what stopped it. Empty
    when the read is stale for another reason, or not stale at all."""

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


# What a tip operation cannot speak for: it names the channels it used, and an
# abort's lost tips may be on any of the others.
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
    to be looked at over a state it now knows with certainty. The gap an abort
    leaves is the exception: a pick speaks only about the channels it used, and
    tips the abort left on the others are still unaccounted for. Only an
    operator saying what is mounted, or a discard that takes everything off,
    settles that one.
    """
    carried: dict[int, MountedTip] = {}
    seen_any = False
    gaps: set[ObservationGapCause] = set()

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
            carried = {
                channel: MountedTip(rack, position, channel_is_inferred=False)
                for channel, (rack, position) in details.by_channel.items()
            }
        elif isinstance(details, MountedTipsConfirmedDetails):
            seen_any = True
            gaps.clear()
        elif isinstance(details, HeadObservationGapDetails):
            gaps.add(details.cause)

    if not seen_any:
        return MountedTips({}, Provenance.UNKNOWN)
    if not gaps:
        return MountedTips(carried, Provenance.KNOWN)
    return MountedTips(carried, Provenance.STALE, frozenset(gaps))


class MountedTipsLedger:
    """What each head is carrying, owned by the record rather than the driver."""

    def __init__(self, ops_history: OpsHistory) -> None:
        self._ops_history = ops_history

    async def of(self, device_name: str) -> MountedTips:
        """What the record says, and never more confidence than it has.

        An action that has picked tips up and not yet finished is holding those
        records, so the fold is behind. The count stays what the record says --
        an unfinished action may still be retried, and counting its operations
        twice would be a different wrong answer -- but the read stops calling
        itself known, which is what the tip pre-flight acts on.
        """
        ops = await ops_for_device(self._ops_history.store, device_name)
        mounted = fold_mounted(ops, device_name)
        if (
            mounted.provenance is Provenance.KNOWN
            and self._ops_history.unrecorded.touches_device(device_name)
        ):
            return MountedTips(mounted.by_channel, Provenance.STALE)
        return mounted

    async def assert_mounted(
        self, device_name: str, by_channel: dict[int, tuple[str, str]],
    ) -> None:
        """An operator says this is what is on the head, channel by (rack,
        position). Absolute: an empty map says it is carrying nothing."""
        await self._ops_history.append_set_mounted_tips(device_name, by_channel)

    async def note_observation_gap(
        self, device_name: str, cause: ObservationGapCause,
    ) -> None:
        """Nobody was watching this head for a while. Moves no tip; expires an
        attestation."""
        await self._ops_history.append_head_observation_gap(device_name, cause)

    async def confirm(self, device_name: str) -> None:
        """The operator agrees with what the record already says.

        Writes its own record, so agreement is a statement and not merely the
        absence of a correction. It restates no tips: the operator agreed the
        head carries these, and was never shown which channel numbers the
        record had guessed, so guessed ones stay marked.
        """
        mounted = await self.of(device_name)
        if mounted.provenance is Provenance.UNKNOWN:
            raise ValueError(
                f"nothing has ever said what {device_name!r} is carrying, so "
                f"there is nothing to confirm; state it with set_mounted_tips"
            )
        if self._ops_history.unrecorded.touches_device(device_name):
            raise ValueError(
                f"an unfinished action has already picked up or dropped tips on "
                f"{device_name!r} and the record has not been told, so there is "
                f"nothing here worth agreeing with; settle the action (retry, "
                f"continue or abort) and the record catches up on its own"
            )
        if mounted.gaps & GAPS_THAT_LOST_WORK:
            raise ValueError(
                f"an aborted action lost the record of tips it moved on "
                f"{device_name!r}, so agreeing with this read would write down a "
                f"head that may be carrying more than it says; look at the head "
                f"and state it with set_mounted_tips"
            )
        await self._ops_history.append_confirm_mounted_tips(device_name)
