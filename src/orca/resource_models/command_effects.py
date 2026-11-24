"""What a device command states it does to labware, and the records that follow from it.

A command states its labware motion and the site it tells, as data. `LabwarePositionEffect` and `DeckRecordEffect`
read that data, never the command's name, so a run's move and an operator's pick or place leave the same records.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypeVar

from pydantic import JsonValue

from orca.gateway.controller.command_pipeline import Check, DeviceCommandPipeline
from orca.gateway.controller.exceptions import CommandNotSavedError
from orca.resource_models.deck_access import device_behind
from orca.resource_models.device_error import SlotOccupiedError
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placeable_interface import IPlateMover
from orca.resource_models.labware_placement import Arrival, LabwarePlacer, Relocation
from orca.resource_models.location import Location
from orca.resource_models.transporter_base import TransporterBase
from orca.resource_models.unheld_command import StoredMotion, StoredSiteNotice, UnheldCommand
from orca.resource_models.vendor_protocol_head import VendorProtocolHeadCheck
from orca.events.emitter import IEventEmitter
from orca.state.command_operations import args_text
from orca.state.records import ObservationGapCause

SaveByName = Callable[[str, str, str], Awaitable[None]]
T = TypeVar("T")


class Carried(Enum):
    """Where a completed command put its labware."""

    PICKED_UP = "picked_up"
    SET_DOWN = "set_down"
    MOVED_BY_DEVICE = "moved_by_device"


class SiteEvent(Enum):
    PICKED = "picked"
    PLACED = "placed"


@dataclass(frozen=True)
class LabwareMotion:
    """The labware a command moves: from `source` to `target` when it completes.

    `failed_back_to` is where the labware is when the command fails, when that is not where the record has it.
    """

    labware: LabwareInstance
    source: Location | None
    target: Location
    carried: Carried
    failed_back_to: Location | None


@dataclass(frozen=True)
class SiteNotice:
    """The site whose own record the command changes when it completes. No labware when none is on record there."""

    site: Location
    labware: LabwareInstance | None
    mover: IPlateMover
    event: SiteEvent


@dataclass(frozen=True)
class CommandStatement:
    device_name: str
    motion: LabwareMotion | None
    site: SiteNotice | None


ReportTargetOccupied = Callable[[str, LabwareMotion, SlotOccupiedError], None]


def arm_pick(mover: TransporterBase, site: Location, labware: LabwareInstance) -> CommandStatement:
    """A pick: on completed the labware at the site is in the jaws, and the site is told; on failed it stays."""
    return CommandStatement(
        device_name=mover.name,
        motion=LabwareMotion(labware, site, mover.gripper_location, Carried.PICKED_UP, failed_back_to=None),
        site=SiteNotice(site, labware, mover, SiteEvent.PICKED),
    )


def arm_place(
    mover: TransporterBase, site: Location, labware: LabwareInstance, picked_from: Location | None,
) -> CommandStatement:
    """A place: on completed the labware in the jaws is at the site, and the site is told.

    On failed it stays in the jaws, unless the mover carries the plate in its one place call: then the plate never
    left `picked_from`.
    """
    back_to = None if mover.pick_moves_the_plate else picked_from
    return CommandStatement(
        device_name=mover.name,
        motion=LabwareMotion(labware, mover.gripper_location, site, Carried.SET_DOWN, failed_back_to=back_to),
        site=SiteNotice(site, labware, mover, SiteEvent.PLACED),
    )


def device_move(
    device_name: str, labware: LabwareInstance, source: Location | None, target: Location,
) -> CommandStatement:
    """A device moved the labware in one call and already knows: on completed it is at the target; on failed it stays.

    A labware with no present position has no source: the move is what puts it down.
    """
    return CommandStatement(
        device_name=device_name,
        motion=LabwareMotion(labware, source, target, Carried.MOVED_BY_DEVICE, failed_back_to=None),
        site=None,
    )


class LabwarePositionEffect:
    """Where the labware is. Follows the hardware even when the command's record failed to save.

    When the record already has another labware at the target, nothing is written: the conflict is reported and the
    command fails as not saved.
    """

    def __init__(self, placer: LabwarePlacer, report_target_occupied: ReportTargetOccupied) -> None:
        self._placer = placer
        self._report_target_occupied = report_target_occupied

    @property
    def runs_after_a_failed_save(self) -> bool:
        return True

    async def completed(self, statement: CommandStatement) -> None:
        motion = statement.motion
        if motion is None:
            return
        try:
            await self._record(motion)
        except SlotOccupiedError as occupied:
            # Two records that cannot both be true, which only a person can settle. Nothing was written, so the
            # command fails as not saved.
            self._report_target_occupied(statement.device_name, motion, occupied)
            raise

    async def _record(self, motion: LabwareMotion) -> None:
        if motion.source is None:
            await self._placer.arrived(motion.labware, motion.target, Arrival.PLACED)
        elif motion.carried is Carried.PICKED_UP:
            await self._placer.picked_up(motion.labware, motion.target)
        elif motion.carried is Carried.SET_DOWN:
            await self._placer.set_down(motion.labware, motion.target, motion.source)
        elif motion.source is not motion.target:
            await self._placer.relocate(motion.labware, motion.source, motion.target, Relocation.BY_THE_DEVICE)

    async def failed(self, statement: CommandStatement) -> None:
        motion = statement.motion
        if motion is None or motion.failed_back_to is None or motion.source is None:
            return
        await self._placer.put_back(motion.labware, motion.failed_back_to, motion.source)

    async def no_answer(self, statement: CommandStatement) -> None:
        """Nobody knows what the command did, so the record stays and says so."""
        if statement.motion is not None:
            await statement.motion.labware.note_observation_gap(ObservationGapCause.OPERATIONS_DROPPED)


class DeckRecordEffect:
    """What the site's own device holds. Not told when an earlier record failed; the next rebuild tells it."""

    @property
    def runs_after_a_failed_save(self) -> bool:
        return False

    async def completed(self, statement: CommandStatement) -> None:
        notice = statement.site
        if notice is None:
            return
        if notice.event is SiteEvent.PICKED:
            await notice.site.notify_picked(notice.labware, notice.mover)
        else:
            await notice.site.notify_placed(notice.labware, notice.mover)

    async def failed(self, statement: CommandStatement) -> None:
        return None

    async def no_answer(self, statement: CommandStatement) -> None:
        return None


class IStatementLookup(Protocol):
    """Finds again what a stored statement names."""

    def labware_with_id(self, labware_id: str) -> LabwareInstance | None: ...

    def location_at(self, position_id: str) -> Location | None: ...

    def mover_named(self, name: str) -> IPlateMover | None: ...


def stated_after_a_failure(statement: CommandStatement | None) -> CommandStatement | None:
    """What mark complete can still apply once a command's failed effects ran.

    Nothing when they put the labware back on its source: the completed effect starts from the jaws, and the
    labware is no longer there on the record.
    """
    if statement is None or statement.motion is None or statement.motion.failed_back_to is None:
        return statement
    return CommandStatement(device_name=statement.device_name, motion=None, site=None)


def stored_command(device_name: str, statement: CommandStatement | None) -> UnheldCommand:
    """What a command states, by id, so its incident can settle it after a restart."""
    motion = None if statement is None else statement.motion
    site = None if statement is None else statement.site
    return UnheldCommand(
        device_name=device_name,
        motion=None if motion is None else StoredMotion(
            labware_id=motion.labware.id,
            source_position_id=None if motion.source is None else motion.source.position_id,
            target_position_id=motion.target.position_id, carried=motion.carried.value,
        ),
        site=None if site is None else StoredSiteNotice(
            position_id=site.site.position_id, labware_id=None if site.labware is None else site.labware.id,
            mover_name=site.mover.name,
            event=site.event.value,
        ),
    )


def restored_statement(unheld: UnheldCommand, lookup: IStatementLookup) -> CommandStatement:
    """The statement a stored command names, with any part whose labware, site or mover is gone left out."""
    motion: LabwareMotion | None = None
    if unheld.motion is not None:
        labware = lookup.labware_with_id(unheld.motion.labware_id)
        target = lookup.location_at(unheld.motion.target_position_id)
        source = (
            None if unheld.motion.source_position_id is None else lookup.location_at(unheld.motion.source_position_id)
        )
        if labware is not None and target is not None:
            motion = LabwareMotion(labware, source, target, Carried(unheld.motion.carried), failed_back_to=None)
    site: SiteNotice | None = None
    if unheld.site is not None:
        stated = None if unheld.site.labware_id is None else lookup.labware_with_id(unheld.site.labware_id)
        location = lookup.location_at(unheld.site.position_id)
        mover = lookup.mover_named(unheld.site.mover_name)
        known = unheld.site.labware_id is None or stated is not None
        if known and location is not None and mover is not None:
            site = SiteNotice(location, stated, mover, SiteEvent(unheld.site.event))
    return CommandStatement(device_name=unheld.device_name, motion=motion, site=site)


class SystemCommandPipeline(DeviceCommandPipeline[CommandStatement]):
    """The system's one pipeline: the record, then labware positions, then the device deck record.

    A pick from a device's site records both under that device's lock. The position frees the site first, and a
    mover granted the site waits on the lock until the device has been told the plate left.
    """

    def __init__(
        self, placer: LabwarePlacer, save_by_name: SaveByName, report_target_occupied: ReportTargetOccupied,
        labware_at_device: Callable[[str], list[LabwareInstance]],
        lookup: IStatementLookup,
        vendor_protocol_head: VendorProtocolHeadCheck,
    ) -> None:
        self._deck_record = DeckRecordEffect()
        super().__init__([LabwarePositionEffect(placer, report_target_occupied), self._deck_record])
        self._placer = placer
        self._save_by_name = save_by_name
        self._labware_at_device = labware_at_device
        self._lookup = lookup
        self._vendor_protocol_head = vendor_protocol_head

    async def run(
        self,
        device_name: str,
        command: str,
        send: Callable[[], Awaitable[T]],
        record: Callable[[T], Awaitable[None]],
        *,
        statement: CommandStatement | None,
        checks: Sequence[Check],
        params: Mapping[str, JsonValue] | None,
        execution_id: str | None,
        events: IEventEmitter | None,
        carries_out: tuple[str, str] | None,
        named: Sequence[str] = (),
        hold: AbstractAsyncContextManager[None] | None = None,
        world_upkeep: bool = False,
    ) -> T:
        """Every sender's command passes the same checks: a vendor protocol also needs an empty head."""
        return await super().run(
            device_name, command, send, record, statement=statement,
            checks=(*self._vendor_protocol_head.checks_for(device_name, command), *checks), params=params,
            execution_id=execution_id, events=events, carries_out=carries_out, named=named, hold=hold,
            world_upkeep=world_upkeep,
        )

    def _held_while_recorded(self, statement: CommandStatement) -> AbstractAsyncContextManager[None]:
        notice = statement.site
        if notice is None or notice.event is not SiteEvent.PICKED:
            return nullcontext()
        device = device_behind(notice.site)
        if device is None:
            return nullcontext()
        return device.lock.held_for(f"recording the pick from {notice.site.position_id}")

    @property
    def placer(self) -> LabwarePlacer:
        return self._placer

    def labware_at(self, device_name: str) -> list[LabwareInstance]:
        return self._labware_at_device(device_name)

    async def settle(self, unheld: UnheldCommand, completed: bool, filed_at: float) -> bool:
        """A person's decision on a command whose outcome is unknown: completed applies what it states; failed
        leaves the record as it is, flagged.

        Completed applies nothing when the labware has moved since the incident was filed at `filed_at`: it is
        somewhere else, or it was moved and has come back. A retry or a later move already settled where it is, and
        replaying the stored move would undo that. Returns True when it applied nothing for that reason.
        """
        if not completed:
            return False
        if self._has_moved_since(unheld, filed_at):
            return True
        await self.apply_completed(restored_statement(unheld, self._lookup))
        return False

    def _has_moved_since(self, unheld: UnheldCommand, filed_at: float) -> bool:
        if unheld.motion is None:
            return False
        labware = self._lookup.labware_with_id(unheld.motion.labware_id)
        if labware is None:
            return False
        if self._placer.present_position_of(labware) != unheld.motion.source_position_id:
            return True
        return self._placer.position_written_after(labware, filed_at)

    async def tell_the_site_again(self, notice: SiteNotice) -> None:
        """Make a deck-record effect a command owes, after its record failed to save and the command is retried."""
        await self._deck_record.completed(CommandStatement(device_name=notice.mover.name, motion=None, site=notice))

    def record_by_name(
        self, device_name: str, command: str, params: Mapping[str, JsonValue],
    ) -> Callable[[None], Awaitable[None]]:
        """The record the engine saves for an arm command, which names no labware of its own."""
        async def record(_result: None) -> None:
            await self._save_by_name(device_name, command, args_text(params))
        return record


def reach_params(position_id: str) -> dict[str, JsonValue]:
    """What an arm's pick or place at a site is sent and recorded with, by a run or an operator."""
    return {"teachpoint": {"position_id": position_id}}


@dataclass(frozen=True)
class Sender:
    """Who sends an arm command, and how its record is read."""

    record: Callable[[None], Awaitable[None]]
    checks: tuple[Check, ...]
    execution_id: str | None
    events: IEventEmitter | None


async def pick_with_the_arm(
    pipeline: SystemCommandPipeline, mover: TransporterBase, site: Location, labware: LabwareInstance, sender: Sender,
) -> None:
    """The one way an arm picks, for a run's move and an operator's pick alike.

    A mover whose place carries the plate the whole way moved nothing on its pick. When a record of that pick
    fails, the plate is put back on the site and the failure is the pick's own: nothing ran that must not be resent.
    """
    try:
        async with mover.holding_the_site_through_its_records(site, "pick_at_coords"):
            await pipeline.run(
                mover.name, "pick_at_coords", lambda: mover.pick(site), sender.record,
                statement=arm_pick(mover, site, labware), checks=sender.checks, params=None,
                execution_id=sender.execution_id, events=sender.events, carries_out=None,
            )
    except CommandNotSavedError as not_saved:
        if mover.pick_moves_the_plate:
            raise
        await pipeline.placer.put_back(labware, site, mover.gripper_location)
        original = not_saved.__cause__
        if isinstance(original, Exception):
            raise original
        raise


async def reach_with_nothing_on_record(
    pipeline: SystemCommandPipeline, mover: TransporterBase, site: Location, command: str, sender: Sender,
) -> None:
    """An operator's pick or place where the record has no plate: the device's default grip, and no position moves.

    The site is still told, so a device's door closes after the reach; its record names no labware.
    Only an operator sends this; a run's move refuses an empty source.
    """
    picking = command == "pick_at_coords"
    statement = CommandStatement(
        device_name=mover.name, motion=None,
        site=SiteNotice(site, None, mover, SiteEvent.PICKED if picking else SiteEvent.PLACED),
    )
    reach = mover.pick_with_nothing_on_record if picking else mover.place_with_nothing_on_record
    await pipeline.run(
        mover.name, command, lambda: reach(site), sender.record,
        statement=statement, checks=sender.checks, params=None,
        execution_id=sender.execution_id, events=sender.events, carries_out=None,
    )


async def place_with_the_arm(
    pipeline: SystemCommandPipeline, mover: TransporterBase, site: Location, labware: LabwareInstance,
    picked_from: Location | None, sender: Sender,
) -> None:
    """The one way an arm places, for a run's move and an operator's place alike."""
    await pipeline.run(
        mover.name, "place_at_coords", lambda: mover.place(site), sender.record,
        statement=arm_place(mover, site, labware, picked_from), checks=sender.checks, params=None,
        execution_id=sender.execution_id, events=sender.events, carries_out=None,
    )
