"""LabwarePlacer: the one writer of where labware is.

Its calls are named by what happened: ``arrived`` and ``left`` for labware
entering and leaving the system, ``picked_up``, ``set_down`` and ``put_back``
for a move, ``relocate`` for a device that moved it in one call or an operator
stating where it is. Each writes every position holder downstream readers use:
the location slot and the staging-bridge loaded-list, the position ledger, the
saved position in the labware service, and the liquid handler's deck (this one
plate, never the whole deck).

The transporter world graph is deliberately NOT written here: a deck resident
lives on a child site, not the routing node (the device handoff), which must
stay free for transit. The bridge presents the plate at the handoff transiently
when it is picked, so the routing graph needs no placement-time write.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from enum import Enum
from typing import Protocol

from typing_extensions import Self

from orca.resource_models.device_error import SlotOccupiedError
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_location_service import ArrivalMechanism, ILabwareLocationService
from orca.resource_models.labware_service_interface import ILabwareService
from orca.resource_models.labware_staging_bridge import LabwareStagingBridge
from orca.resource_models.location import Location
from orca.resource_models.resources import ILabwareStateHolder

logger = logging.getLogger(__name__)


class ProjectLabware(Protocol):
    """Make each distinct liquid-handler deck these locations sit on agree about
    this ONE labware, leaving everything else on those decks alone."""

    async def __call__(
        self, labware: LabwareInstance, *locations: Location,
    ) -> None: ...


class WorldsGoStale(Protocol):
    """Mark driver worlds this write did not reach, so the next command to each device rebuilds its world first."""

    async def __call__(self) -> None: ...


class RebuildDeck(Protocol):
    """Make the liquid handler at this location hold what the ledger places on its deck, and nothing else."""

    async def __call__(self, device_location: Location) -> None: ...


class PositionNotSavedError(Exception):
    """The ledger has the labware's new position, and saving it to the labware service failed.

    A restart would bring the labware back where the database last had it. ``labware_id`` is None when every
    labware's saved position was being cleared.
    """

    def __init__(self, message: str, labware_id: str | None) -> None:
        super().__init__(message)
        self.labware_id = labware_id

    @classmethod
    def of(cls, labware: LabwareInstance, cause: Exception) -> Self:
        return cls(
            f"{labware.name} ({labware.id}) is where the record says, but saving its position failed ({cause}); "
            f"state where it is again once the database is reachable.",
            labware.id,
        )

    @classmethod
    def of_departure(cls, labware: LabwareInstance, cause: Exception) -> Self:
        return cls(
            f"{labware.name} ({labware.id}) left the record, but clearing its saved position failed ({cause}); "
            f"discharge it again once the database is reachable, or a restart brings it back.",
            labware.id,
        )

    @classmethod
    def of_shutdown(cls, labware: LabwareInstance, seconds: float) -> Self:
        return cls(
            f"saving where {labware.name} ({labware.id}) is had not finished {seconds:g}s into the shutdown and was "
            f"stopped; after the restart, state where it is.",
            labware.id,
        )

    @classmethod
    def of_every_labware(cls, cause: Exception) -> Self:
        return cls(
            f"the record is cleared, but clearing the saved positions failed ({cause}); "
            f"clear all labware again once the database is reachable.",
            None,
        )


class IUnsavedPositions(Protocol):
    """Where a save that failed after its caller was cancelled goes, since nobody is left to raise it to."""

    def report(self, labware: LabwareInstance, error: PositionNotSavedError) -> None: ...


class _Turn:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


class TurnPerLabware:
    """One caller per labware at a time, in the order they asked.

    An asyncio lock queues waiters first in, first out, and a waiter that is cancelled leaves the queue. A
    labware's lock is dropped once nobody holds or waits on it.
    """

    def __init__(self) -> None:
        self._turns: dict[str, _Turn] = {}

    def __len__(self) -> int:
        """How many labware have a turn held or waited on."""
        return len(self._turns)

    @asynccontextmanager
    async def turn(self, labware_id: str) -> AsyncIterator[None]:
        turn = self._turns.setdefault(labware_id, _Turn())
        turn.users += 1
        try:
            async with turn.lock:
                yield
        finally:
            turn.users -= 1
            if turn.users == 0:
                del self._turns[labware_id]


class ForgetLabware(Protocol):
    """Drop this ONE labware from the system's list of labware; its history stays in the record."""

    def __call__(self, labware: LabwareInstance) -> None: ...


class Arrival(Enum):
    """How labware came to be at a site; see ``LabwarePlacer.arrived``."""

    PLACED = "placed"
    STATED = "stated"
    RESTATED = "restated"
    SPAWNED = "spawned"
    RESTORED = "restored"
    BOUND = "bound"


class Relocation(Enum):
    """Who says labware moved; see ``LabwarePlacer.relocate``."""

    BY_THE_DEVICE = "by_the_device"
    STATED = "stated"
    RESTATED = "restated"


class LabwarePlacer:
    def __init__(
        self,
        location_service: ILabwareLocationService,
        labware_service: ILabwareService,
        unsaved_positions: IUnsavedPositions,
        project_labware: ProjectLabware,
        other_worlds_go_stale: WorldsGoStale,
        every_world_goes_stale: WorldsGoStale,
        rebuild_deck: RebuildDeck,
        forget: ForgetLabware,
    ) -> None:
        self._location_service = location_service
        self._labware_service = labware_service
        self._unsaved_positions = unsaved_positions
        self._project_labware = project_labware
        self._other_worlds_go_stale = other_worlds_go_stale
        self._every_world_goes_stale = every_world_goes_stale
        self._rebuild_deck = rebuild_deck
        self._forget = forget
        self._restored_at: dict[str, float] = {}
        # One labware's saves commit in the order the ledger took them: a caller enters right after its ledger
        # write, with no await between.
        self._saves = TurnPerLabware()
        self._writing: dict[asyncio.Task[None], LabwareInstance] = {}

    async def _then_save(
        self, labware: LabwareInstance, save: Callable[[], Awaitable[None]],
        others: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Write the other holders, then save, in this plate's turn. Called with no await since the ledger write.

        The writes run as their own task, started in ledger order, so a caller cancelled while it waits for the turn
        is still cancelled but its save still lands. A failure nobody is left to receive is reported.
        """
        writes = asyncio.create_task(self._in_turn(labware, save, others))
        self._writing[writes] = labware
        writes.add_done_callback(self._writing.pop)
        try:
            await asyncio.shield(writes)
        except asyncio.CancelledError:
            if not writes.done():
                writes.add_done_callback(lambda done: self._report_if_failed(labware, done))
            raise

    async def finish_saves(self, seconds: float) -> None:
        """Wait for every save still being written, so what a restart reads includes them.

        One still running after ``seconds`` is stopped and reported, never dropped.
        """
        writing = dict(self._writing)
        if not writing:
            return
        _done, late = await asyncio.wait(writing, timeout=seconds)
        for task in late:
            task.cancel()
            self._unsaved_positions.report(writing[task], PositionNotSavedError.of_shutdown(writing[task], seconds))

    async def _in_turn(
        self, labware: LabwareInstance, save: Callable[[], Awaitable[None]],
        others: Callable[[], Awaitable[None]] | None,
    ) -> None:
        async with self._saves.turn(labware.id):
            try:
                if others is not None:
                    await others()
            finally:
                await save()

    def _report_if_failed(self, labware: LabwareInstance, writes: asyncio.Future[None]) -> None:
        if writes.cancelled():
            return
        error = writes.exception()
        if error is None:
            return
        if isinstance(error, PositionNotSavedError):
            self._unsaved_positions.report(labware, error)
            return
        logger.error("writing where %s is failed after its caller was cancelled: %s", labware.name, error)

    async def _save(self, labware: LabwareInstance, at: Location) -> None:
        try:
            if await self._labware_service.get_by_id(labware.id) is None:
                await self._labware_service.register(labware)
            await self._labware_service.update_location(labware.id, at.position_id)
        except Exception as cause:
            raise PositionNotSavedError.of(labware, cause) from cause

    async def _save_departure(self, labware: LabwareInstance) -> None:
        try:
            await self._labware_service.clear_location(labware.id)
        except Exception as cause:
            raise PositionNotSavedError.of_departure(labware, cause) from cause

    async def arrived(self, labware: LabwareInstance, at: Location, how: Arrival) -> None:
        """``labware`` is at ``at``. ``how`` it got there decides what is written, and in what order.

        - PLACED: a register, or the engine putting back a plate it knows of. The slot, then the device, which
          gives the slot back if it refuses, then the ledger.
        - STATED: an operator says where a plate with no present position is. As PLACED, and then every other
          run-mode world is marked for a rebuild, since one may still hold it where it last was.
        - RESTATED: as STATED, and the ledger starts its history over.
        - SPAWNED: a spawn or a dispense put it there physically. Slot and ledger first, then the device, whose
          refusal never undoes it: undoing would let the next dispense jam the output.
        - RESTORED: after a restart. Slot and ledger only: a device's orca-client may not be connected yet, and
          every world starts marked, so each is rebuilt from the ledger before its next command. The saved position is
          where it came from, so it is not written again.
        - BOUND: a reuse-bind whose slot ``claim`` already took inside its lock. The device is told, the site
          announces it, then the ledger.

        Every arrival but RESTORED saves the position last, after every other holder, when the ledger says it
        changed. A holder that fails still lets the save run, and the save's own failure is raised after them.
        """
        if how is Arrival.RESTORED:
            # The slot's observers include each transporter that reaches it, and telling one is a device command.
            at.initialize_labware(labware)
        elif how is not Arrival.BOUND:
            await at.place_labware(labware)
        if how in (Arrival.PLACED, Arrival.STATED, Arrival.RESTATED):
            try:
                await self._project_labware(labware, at)
            except Exception:
                await at.dispose_labware(labware)
                raise
        if how is Arrival.BOUND:
            await self._project_labware(labware, at)
            await at.notify_initialized(labware)
        if how is Arrival.RESTATED:
            changed = self._location_service.reset(labware, at)
        else:
            changed = self._location_service.update(labware, at)
        if how is Arrival.RESTORED:
            self._restored_at[labware.id] = self._last_written(labware)
            return
        async def others() -> None:
            if how is Arrival.SPAWNED:
                await self._project_labware(labware, at)
            if how in (Arrival.STATED, Arrival.RESTATED):
                await self._other_worlds_go_stale()
        if changed:
            await self._then_save(labware, lambda: self._save(labware, at), others)
        else:
            await others()

    def present_position_of(self, labware: LabwareInstance) -> str | None:
        """Where the record has ``labware`` now, or None when it has no present position."""
        try:
            return self._location_service.get(labware).position_id
        except KeyError:
            return None

    def position_written_after(self, labware: LabwareInstance, when: float) -> bool:
        """Whether the record wrote where ``labware`` is after ``when``. A restart putting it back where it was
        stored is not a new position."""
        restored = self._restored_at.get(labware.id)
        return any(written > when and written != restored for written in self._written(labware))

    def _written(self, labware: LabwareInstance) -> list[float]:
        try:
            history = self._location_service.get_history(labware)
        except KeyError:
            return []
        return [written for _location, written in history.get_history_with_timestamps()]

    def _last_written(self, labware: LabwareInstance) -> float:
        return self._written(labware)[-1]

    def claim(self, labware: LabwareInstance, at: Location) -> None:
        """Take ``at`` for ``labware`` at once, inside the caller's lock, so a contender sees it bound.

        Tells nobody: ``arrived(..., Arrival.BOUND)`` finishes the arrival outside the lock.
        """
        at.initialize_labware(labware)

    async def confirmed(self, labware: LabwareInstance, at: Location) -> None:
        """Someone says ``labware`` is where the record already has it. Nothing moves; the saved position is
        written again, which stamps when it was last confirmed."""
        await self._then_save(labware, lambda: self._save(labware, at))

    def expected(self, labware: LabwareInstance, at: Location, mechanism: ArrivalMechanism) -> None:
        """``labware`` belongs at ``at`` and has not arrived."""
        self._location_service.expect(labware, at, mechanism)

    def no_longer_expected(self, labware: LabwareInstance) -> None:
        """``labware`` will not arrive: a thread stopped before its start labware came."""
        self._location_service.stop_expecting(labware)

    async def left(self, labware: LabwareInstance, at: Location | None) -> None:
        """``labware`` left the system from ``at``: a thread's MANUAL_REMOVE end, or a discharge.

        The slot lets it go if it still holds it, the ledger retires it so a
        restart does not bring it back, the system stops listing it (its history
        stays), and then the device takes it off its deck. In that order, so a
        device that cannot be reached leaves the record right. The saved position
        is cleared last, whatever failed before it.

        ``at`` is None for labware no slot holds any more: an operator's discharge
        after each holder let it go. It is then no longer expected either, its
        saved position is cleared whatever the ledger said, and every world of
        every device is marked for a rebuild, since a thread that ended may have
        left it standing on a deck nothing points at.
        """
        try:
            if at is not None and at.labware is labware:
                await at.dispose_labware(labware)
        except BaseException:
            retired = self._location_service.retire(labware)
            self._location_service.stop_expecting(labware)
            self._forget(labware)
            if retired:
                await self._then_save(labware, lambda: self._save_departure(labware))
            raise
        saves = self._location_service.retire(labware) or at is None
        self._location_service.stop_expecting(labware)
        self._forget(labware)

        async def others() -> None:
            if at is not None:
                await self._project_labware(labware, at)
                return
            await self._every_world_goes_stale()
        if saves:
            await self._then_save(labware, lambda: self._save_departure(labware), others)
        else:
            await others()

    async def cleared_all(self, locations: Iterable[Location], holders: Iterable[ILabwareStateHolder]) -> list[str]:
        """Every labware left the system at once: the panic clear. Returns the ids whose saved position it cleared.

        The ledger is wiped, then every saved position, including labware the ledger never knew, which would
        otherwise come back on the next boot. Then each staging bridge and each holder's projection resets to it. A
        holder that cannot be reached is logged and skipped: the record is already empty, and the clear is what an
        operator reaches for when a device is down. Every world is then marked, so a skipped one is rebuilt later.
        """
        self._location_service.clear_all()
        cleared = await self._clear_every_saved_position()
        for location in locations:
            # A bridge keeps a loaded list beside the slot, so emptying slots can miss a plate a device holds.
            if isinstance(location.resource, LabwareStagingBridge):
                location.resource.reset_loaded_labware()
        for holder in holders:
            try:
                await holder.reset_labware_state_everywhere()
            except Exception as exc:
                logger.warning("clear-all could not reset the labware state of %s: %s", holder.name, exc)
        await self._every_world_goes_stale()
        return cleared

    async def _clear_every_saved_position(self) -> list[str]:
        cleared: list[str] = []
        try:
            saved = await self._labware_service.list_active_locations()
        except Exception as cause:
            raise PositionNotSavedError.of_every_labware(cause) from cause
        for labware_id, _position_id in saved:
            try:
                await self._labware_service.clear_location(labware_id)
            except Exception as cause:
                raise PositionNotSavedError.of_every_labware(cause) from cause
            cleared.append(labware_id)
        return cleared

    async def jaws_released(self, mover: ILabwareStateHolder) -> None:
        """An operator took the plate out of a mover's jaws: they hold nothing, and the pick origin goes with it."""
        await mover.reset_labware_state()

    async def contents_set(self, labware: LabwareInstance, at: Location) -> None:
        """Someone stated what ``labware`` at ``at`` holds: the deck it stands on is told about it alone."""
        await self._project_labware(labware, at)

    async def deck_rebuilt(self, device_location: Location) -> None:
        """A liquid handler's driver lost its deck: it is told again everything the ledger places there."""
        await self._rebuild_deck(device_location)

    async def picked_up(self, labware: LabwareInstance, jaws: Location) -> None:
        """The labware is in the jaws. Called the instant the pick returns, under the site's device lock.

        So its save never gains an `others` step: that takes every handler's lock inside the plate's turn, the
        opposite order, and would deadlock with an operator's relocate.

        Before the source is told, because that is a device call that can fail
        and the plate is already off the source when it does.
        """
        await jaws.place_labware(labware)
        if self._location_service.update(labware, jaws):
            await self._then_save(labware, lambda: self._save(labware, jaws))

    async def set_down(
        self, labware: LabwareInstance, at: Location, jaws: Location,
    ) -> None:
        """The labware is out of the jaws and at ``at``.

        When the record already has another labware at ``at``, nothing is written and ``SlotOccupiedError`` names it.
        """
        occupant = at.labware
        if occupant is not None and occupant.id != labware.id:
            raise SlotOccupiedError(at.position_id, occupant.name, occupant.template_name)
        await jaws.dispose_labware(labware)
        if self._location_service.update(labware, at):
            await self._then_save(labware, lambda: self._save(labware, at))

    async def put_back(
        self, labware: LabwareInstance, source: Location, jaws: Location,
    ) -> None:
        """The transfer never happened: the labware is back on ``source``.

        For a mover whose driver carries the plate in one call, a refused place
        means nothing ever left the slot. The slot is written before the jaws
        are cleared so the labware is never at no place at all, and the ledger
        last, matching ``arrived``. No device projection: the driver never lost
        the plate, so it has nothing to be told.
        """
        await source.place_labware(labware)
        await jaws.dispose_labware(labware)
        if self._location_service.update(labware, source):
            await self._then_save(labware, lambda: self._save(labware, source))

    async def relocate(
        self, labware: LabwareInstance, source: Location, target: Location, how: Relocation,
    ) -> None:
        """Move ``labware`` from one location to another, whole or not at all.

        Both holders move before anything is projected. The projection reads the
        slots, so projecting a target while the source still holds the same
        labware shows it at two sites at once. When the projection fails for
        its own reasons, both holders go back to where they were. A device that
        made the move itself already knows, so it is not told again.

        Every other run-mode world still holds the plate where it was, and that
        stale copy refuses the next command at that site, so those worlds are marked for a rebuild.
        """
        await target.place_labware(labware)
        try:
            await source.dispose_labware(labware)
            if how is not Relocation.BY_THE_DEVICE:
                await self._project_labware(labware, target, source)
        except Exception:
            await target.dispose_labware(labware)
            await source.place_labware(labware)
            raise
        if how is Relocation.RESTATED:
            changed = self._location_service.reset(labware, target)
        else:
            changed = self._location_service.update(labware, target)
        if changed:
            await self._then_save(
                labware, lambda: self._save(labware, target), self._other_worlds_go_stale,
            )
        else:
            await self._other_worlds_go_stale()
