"""SpawnAction strategy for entry-thread start_location acquisition
and thread-completion end_location release.

The engine's seam for "make this labware physically available at the
thread's start_location" (start side) and "release the slot at the
thread's end_location when the thread terminates" (end side). Dispatch
is FLAG-BASED: `ThreadTemplate.start_*` and `end_*` declare author
intent, the engine reads them via
`select_spawn_action(thread, location_service)` /
`select_end_spawn_action(thread)`. A start-side strategy takes the
location service because every arrival it causes has to be recorded in
the position ledger. Resource-shape auto-detection
(IPlateSource -> auto-dispense) is GONE; the author always writes the
DISPENSE / MANUAL_PLACE / MANUAL_REMOVE / LEAVE_IN_PLACE / REUSE_EXISTING
sentinel.

Start-side strategies (run-mode-aware where indicated):

- :class:`ManualPlaceSpawn` -- bare-string default, explicit
  `start=("loc", MANUAL_PLACE)`. Both worlds run the same transition, the
  thread's labware going from expected at the start_location to present
  there; only the cause differs. CHECK_RUN / SIM write the slot
  immediately (retry on `DeviceBusyError` to serialize multi-thread
  workflows that share a start_location). LIVE waits for the operator's
  `labware_register`, which adopts the expectation this thread holds
  instead of minting a second instance, so there is nothing to swap.
- :class:`DispenseSpawn` -- explicit `start=("loc", DISPENSE)`. Calls
  `device.dispense()` on the IPlateSource backing the location, then
  writes the slot. Run-mode-agnostic: the driver handles sim / wire /
  live dispatch.

End-side strategy:

- :class:`ManualRemoveSpawn` -- bare-string default, explicit
  `end=("loc", MANUAL_REMOVE)`. CHECK_RUN / SIM return at once, and the
  thread's `LabwarePlacer.left` frees the slot. LIVE polls `end_location.labware`,
  waiting for `labware_discharge` to clear the slot.

`LEAVE_IN_PLACE` is handled inline in `_handle_thread_completion`
(no strategy). `REUSE_EXISTING` binds inline in
`ExecutingWorkflow._resolve_reuse_bind`; when that returns None the thread
comes to `select_spawn_action` like any other.

Dispatch happens at thread.start() time inside
:meth:`ExecutingLabwareThread.initialize_labware` (start side) and at
thread completion inside :meth:`_handle_thread_completion` (end side).
Submit-time stays read-only against the topology graph; physical wire
I/O happens inside the execution task.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from orca.devices.device_interfaces import IPlateSource
from orca.gateway.controller.exceptions import CommandNotSavedError, CommandOutcomeUnknownError, DeviceError
from orca.resource_models.error_policy_overrides import PausesUnderAnyPolicyError
from orca.resource_models.device_error import DeviceBusyError, PlateDispenseError
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_location_service import ArrivalMechanism, ILabwareLocationService
from orca.resource_models.labware_placement import Arrival, LabwarePlacer, Relocation
from orca.state.placement import PlacementState
from orca.resource_models.labware_staging_bridge import LabwareStagingBridge
from orca.resource_models.location import Location
from orca.modes.run_modes import WorkflowRunMode
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.workflow_models.labware_threads.labware_thread import (
    LabwareThreadInstance,
)

orca_logger = logging.getLogger("orca")


_RETRY_BACKOFF_S = 0.5


class PlacementReservationAcquirer(Protocol):
    """Blocking-acquire an exclusive location reservation for a spawn placement.

    Satisfied by ``MoveHandler`` (via ``acquire_placement_reservation``). Kept as
    a narrow Protocol so a spawn depends only on the reservation capability, not
    on the whole routing handler, and unit tests can supply a trivial stand-in.
    """

    async def acquire_placement_reservation(
        self, thread_id: str, labware: LabwareInstance, location: Location
    ) -> LocationReservation:
        ...

    async def try_acquire_placement_reservation(
        self, thread_id: str, labware: LabwareInstance, location: Location
    ) -> LocationReservation | None:
        """One attempt, None when somebody else holds it. For a wait whose real
        subject is a person, so it cannot sit inside a blocking acquire."""
        ...


@dataclass(frozen=True)
class SpawnWaitHooks:
    """How a spawn tells its thread that it is waiting on the start location, and that it stopped waiting."""
    blocked: Callable[[], None]
    cleared: Callable[[], None]


class _StartWait:
    """One spawn's wait on its start location: reported once, and ended by the thread's stop."""

    def __init__(self, thread: LabwareThreadInstance | None, hooks: SpawnWaitHooks | None) -> None:
        self._thread = thread
        self._hooks = hooks
        self._waiting = False

    @property
    def stopped(self) -> bool:
        return self._thread is not None and _stopped(self._thread)

    def blocked(self) -> None:
        if not self._waiting:
            self._waiting = True
            if self._hooks is not None:
                self._hooks.blocked()

    def cleared(self) -> None:
        if self._waiting:
            self._waiting = False
            if self._hooks is not None:
                self._hooks.cleared()

    async def reserve(self, acquiring: Awaitable[LocationReservation]) -> LocationReservation | None:
        """The granted reservation, or None when the thread was stopped first."""
        task = asyncio.ensure_future(acquiring)
        try:
            return await self._granted_or_stopped(task)
        except BaseException:
            # A cancelled thread must not keep a grant that landed as it was cancelled.
            if task.done() and not task.cancelled() and task.exception() is None:
                task.result().release_reservation()
            raise
        finally:
            # A stopped or cancelled thread gives up its request, or it takes the slot later and never lets go.
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _granted_or_stopped(self, task: asyncio.Future[LocationReservation]) -> LocationReservation | None:
        done, _ = await asyncio.wait({task}, timeout=_RETRY_BACKOFF_S)
        if done:
            return task.result()
        self.blocked()
        stop_event = self._thread.stop_event if self._thread is not None else None
        if stop_event is None:
            return await task
        stop = asyncio.ensure_future(stop_event.wait())
        try:
            await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
        return task.result() if task.done() else None


async def _place_retrying_on_busy(
    location: Location,
    labware: LabwareInstance,
    context: str,
    placer: LabwarePlacer,
    wait: _StartWait | None = None,
    stoppable: bool = True,
) -> None:
    """Place ``labware`` at ``location``, retrying on ``DeviceBusyError`` until the slot clears or
    the thread is stopped.

    Covers the brief physical-occupancy overlap: a resident plate not yet picked
    still holds the slot.

    The slot write IS the arrival, so it records one: the ledger has held the
    labware as expected here since the thread was built, and nothing else on
    this path would move it to present.
    """
    await _retry_while_busy(
        lambda: placer.arrived(labware, location, Arrival.SPAWNED),
        location, labware, context, wait if wait is not None else _StartWait(None, None), stoppable,
    )


async def _retry_while_busy(
    write: Callable[[], Awaitable[None]],
    location: Location,
    labware: LabwareInstance,
    context: str,
    wait: _StartWait,
    stoppable: bool = True,
) -> None:
    while True:
        try:
            await write()
            wait.cleared()
            return
        except DeviceBusyError:
            if location.labware is labware:
                # The slot took it and the device then refused: the plate is there, so this is not a busy slot.
                raise
            if stoppable and wait.stopped:
                return
            wait.blocked()
            orca_logger.warning(
                "%s: %s busy with %s; retrying for %s",
                context,
                location.name,
                location.labware,
                labware.name,
            )
            await asyncio.sleep(_RETRY_BACKOFF_S)


async def _reserve_run_release(
    location: Location,
    labware: LabwareInstance,
    thread: LabwareThreadInstance,
    acquirer: PlacementReservationAcquirer | None,
    write: Callable[[], Awaitable[None]],
    wait: _StartWait,
) -> None:
    """Hold an exclusive placement reservation on ``location`` for ``thread``
    across ``write`` -- the whole slot write, including any physical dispense that
    precedes the ``place_labware`` -- releasing the instant it returns.

    Holding the reservation across the entire write is what makes spawn placement
    respect in-flight move reservations: when another thread has reserved this
    location and is mid-transit toward it (its plate picked, slot momentarily
    empty), the acquire blocks until that thread releases instead of racing a
    plate into the slot and colliding when the reserved move arrives. Physical
    occupancy (``DeviceBusyError``) then serializes further placement as before.

    ``acquirer is None`` only in isolated unit tests (a spawn built with no
    reservation manager); with no coordinator there is no other thread to contend
    with, so the bare write is the whole behavior.
    """
    if acquirer is None:
        await write()
        return
    reservation = await wait.reserve(
        acquirer.acquire_placement_reservation(thread.id, labware, location)
    )
    if reservation is None:
        return
    try:
        await write()
    finally:
        reservation.release_reservation()


class SpawnAction(ABC):
    """Strategy contract for entry-thread start_location acquisition."""

    @abstractmethod
    async def acquire(self, thread: LabwareThreadInstance) -> None:
        """Make the thread's labware available at the strategy's
        start_location.

        Concrete strategies write the orca-side slot, record the arrival on
        the position ledger, and fire any wire-side calls required to keep the
        physical world in sync. Exceptions bubble to the thread-loop's
        error-pause infrastructure.
        """
        ...


class EndSpawnAction(ABC):
    """Strategy contract for thread-completion end_location release."""

    @abstractmethod
    async def dispose(self, thread: LabwareThreadInstance) -> None:
        """Release the thread's labware from its end_location.

        Sim modes return at once; the thread's `LabwarePlacer.left` frees the slot. LIVE
        mode polls the slot until the operator clears it via
        `labware_discharge`. The caller (`_handle_thread_completion`)
        transitions the thread to COMPLETED after this returns.
        """
        ...


def _may_have_dispensed(error: Exception) -> bool:
    return isinstance(error, (CommandNotSavedError, CommandOutcomeUnknownError)) or (
        isinstance(error, DeviceError) and error.may_have_moved
    )


class DispenseSpawn(SpawnAction):
    """IPlateSource-backed start_location (stacker, hotel) with explicit
    `start=("loc", DISPENSE)`.

    Physical reality: the source physically holds many plates internally,
    but its OUTPUT position is a single slot. Each thread must wait for
    the previous plate to be picked before triggering the next physical
    dispense; otherwise the stacker would advance its queue with the
    previous plate still at the output. Retry-on-busy serializes:
    `source.dispense()` runs between the wait and the orca-side slot
    write so the engine's `LabwareInstance` is fabricated only after the
    hardware has actually produced a plate.

    Run-mode-agnostic: the driver handles sim / wire / live dispatch.
    """

    def __init__(
        self,
        location: Location,
        placer: LabwarePlacer,
        reservation_acquirer: PlacementReservationAcquirer | None = None,
        wait_hooks: SpawnWaitHooks | None = None,
    ) -> None:
        source = _resolve_plate_source(location)
        if source is None:
            raise TypeError(
                f"DispenseSpawn requires an IPlateSource-backed Location; "
                f"got {location.name!r} with resource "
                f"{type(location.resource).__name__}"
            )
        self._location = location
        self._placer = placer
        self._source = source
        self._reservation_acquirer = reservation_acquirer
        self._wait_hooks = wait_hooks

    async def acquire(self, thread: LabwareThreadInstance) -> None:
        labware = thread.labware
        wait = _StartWait(thread, self._wait_hooks)
        # Reserve BEFORE the physical dispense: dispensing advances a plate into the
        # slot, so a reserved-but-mid-transit move must block the dispense too.
        await _reserve_run_release(
            self._location, labware, thread, self._reservation_acquirer,
            lambda: self._dispense_then_place(labware, wait), wait,
        )

    async def _dispense(self) -> None:
        try:
            await self._source.dispense()
        except PausesUnderAnyPolicyError:
            # An offline source already pauses the thread with its own message.
            raise
        except Exception as error:
            if _may_have_dispensed(error):
                # A plate may be at the output already: a retried dispense onto it jams the source.
                raise
            raise PlateDispenseError(self._source.name, error) from error

    async def _dispense_then_place(self, labware: LabwareInstance, wait: _StartWait) -> None:
        # Dispensing onto an occupied output jams the stacker, so the empty check, the
        # dispense and the record of the plate happen under the source's lock. The wait
        # is outside it, because the pick that empties the output takes the same lock.
        while True:
            async with self._source.dispensing():
                if self._location.labware is None:
                    await self._dispense()
                    # The plate is out of the stacker now, so it is recorded whatever a stop says.
                    await _place_retrying_on_busy(
                        self._location, labware, "DispenseSpawn", self._placer, wait, stoppable=False,
                    )
                    return
            if wait.stopped:
                return
            wait.blocked()
            orca_logger.warning(
                "DispenseSpawn: %s output occupied by %s; waiting for %s",
                self._location.name,
                self._location.labware,
                labware.name,
            )
            await asyncio.sleep(_RETRY_BACKOFF_S)


class ManualPlaceSpawn(SpawnAction):
    """Bare-string default and explicit `start=("loc", MANUAL_PLACE)`.

    Both worlds run the same transition -- the thread's labware goes from
    expected at the start_location to present there. What differs is who
    causes it:

    - CHECK_RUN / SIM: the engine writes the slot immediately, retrying
      on `DeviceBusyError` to serialize shared-start_location threads.
    - LIVE: an operator puts the labware down and calls
      `labware_register(template, location=X)`, which adopts the expectation
      this thread is holding rather than minting a second instance. The wait
      ends when the ledger says the labware arrived; nothing is swapped.
    """

    def __init__(
        self,
        location: Location,
        location_service: ILabwareLocationService,
        placer: LabwarePlacer,
        reservation_acquirer: PlacementReservationAcquirer | None = None,
        wait_hooks: SpawnWaitHooks | None = None,
    ) -> None:
        self._location = location
        self._location_service = location_service
        self._placer = placer
        self._reservation_acquirer = reservation_acquirer
        self._wait_hooks = wait_hooks

    async def acquire(self, thread: LabwareThreadInstance) -> None:
        if thread.run_mode is WorkflowRunMode.LIVE:
            await self._acquire_live(thread)
        else:
            await self._acquire_sim(thread)

    def _arrived(self, thread: LabwareThreadInstance) -> bool:
        return self._location_service.placement(thread.labware) is PlacementState.PRESENT

    async def _acquire_sim(self, thread: LabwareThreadInstance) -> None:
        """CHECK_RUN / SIM: put the thread's labware at the slot.

        Reserves the start_location for this thread before writing the slot, so a
        move that has reserved the same location and is mid-transit toward it
        (slot momentarily empty) is not overtaken by the spawn -- the spawn waits
        for the reservation to clear, symmetric with how moves serialize on
        locations. Routes through ``LabwarePlacer.arrived`` so a deck-site-child
        start also lands on the parent bridge's loaded-list (what the pick
        reads), not just the child slot. Retry-on-busy still serializes the brief
        physical-occupancy overlap when a start_location is shared.
        """
        wait = _StartWait(thread, self._wait_hooks)
        await _reserve_run_release(
            self._location, thread.labware, thread, self._reservation_acquirer,
            lambda: _place_retrying_on_busy(
                self._location, thread.labware, "ManualPlaceSpawn (sim)",
                self._placer, wait,
            ),
            wait,
        )

    async def _acquire_live(self, thread: LabwareThreadInstance) -> None:
        """LIVE: wait for the operator to place this thread's labware, holding
        the slot for them while they do.

        Asks the ledger rather than the slot, because the ledger is what the
        placement chokepoint always writes whatever kind of holder the location
        is, and because "did MY labware arrive" is the question -- reading the
        slot would bind whatever turned up, so two threads waiting on one slot
        would both take the first plate.

        The claim is what the sim half has always taken, for the same reason and
        over a shorter wait. Asking a person to put a plate somewhere is a claim
        on the spot: without it the engine can grant that same slot to another
        thread's move, and the operator is handed one surface saying "put a
        plate on pad_1" and another refusing them for it. The claim is retried
        on the same tick that checks for the arrival, so an operator who places
        the plate is never held up by a claim that has nothing left to protect,
        and it is released the moment the labware is there -- physical occupancy
        guards the slot from then on.

        The claim is re-taken whenever it stops being held. `reservation cancel`
        takes a claim out of the manager without the holder doing anything, so a
        wait that noticed nothing would sit there looking parked while holding
        no spot at all, which is the bug this whole change is about. It also
        means cancelling this particular claim buys a person only half a second,
        which is what the refusal message tells them.

        The wait cooperates with `thread.stop_event`: cooperative stop
        (`ExecutingLabwareThread.stop()`) sets the event and the next tick
        exits early. The caller (`initialize_labware`) sees the labware still
        expected and drops the expectation.
        """
        claim: LocationReservation | None = None
        reported_failure = False
        try:
            while not self._arrived(thread):
                if _stopped(thread):
                    return
                if claim is None or claim.is_released or claim.is_displaced:
                    try:
                        claim = await self._try_claim(thread)
                        reported_failure = False
                    except Exception:
                        # Said once: the loop comes back every half second, and
                        # a coordinator that is down stays down while a person
                        # walks over.
                        if not reported_failure:
                            orca_logger.warning(
                                "Could not claim %s for the manual placement of "
                                "%s; waiting for the plate without a claim on "
                                "the spot.",
                                self._location.name, thread.labware.name,
                                exc_info=True,
                            )
                            reported_failure = True
                        claim = None
                await asyncio.sleep(_RETRY_BACKOFF_S)
        finally:
            if claim is not None:
                claim.release_reservation()

    async def _try_claim(
        self, thread: LabwareThreadInstance,
    ) -> LocationReservation | None:
        """One attempt at holding the start_location for this operator wait.

        None whenever there is no coordinator to ask (unit tests build a spawn
        without one) or another thread holds the slot -- a claim that cannot be
        had is not a reason to stop waiting for the plate. The caller treats a
        raise the same way, for the same reason.
        """
        if self._reservation_acquirer is None:
            return None
        return await self._reservation_acquirer.try_acquire_placement_reservation(
            thread.id, thread.labware, self._location,
        )


class RelocateByHandSpawn(ManualPlaceSpawn):
    """Labware already on the deck where no arm reaches its start: a person moves it there.

    LIVE holds the start for the person and waits until the labware is recorded there
    (``labware.edit_location``). Sim plays the person and records a placement, never a carry.
    """

    def __init__(
        self,
        location: Location,
        source: Location,
        location_service: ILabwareLocationService,
        placer: LabwarePlacer,
        reservation_acquirer: PlacementReservationAcquirer | None = None,
        wait_hooks: SpawnWaitHooks | None = None,
    ) -> None:
        super().__init__(location, location_service, placer, reservation_acquirer, wait_hooks)
        self._source = source

    def _arrived(self, thread: LabwareThreadInstance) -> bool:
        here = self._location_service.present_location(thread.labware)
        return here is not None and here.position_id == self._location.position_id

    async def _acquire_sim(self, thread: LabwareThreadInstance) -> None:
        wait = _StartWait(thread, self._wait_hooks)
        await _reserve_run_release(
            self._location, thread.labware, thread, self._reservation_acquirer,
            lambda: _retry_while_busy(
                lambda: self._placer.relocate(thread.labware, self._source, self._location, Relocation.STATED),
                self._location, thread.labware, "RelocateByHandSpawn", wait,
            ),
            wait,
        )


class ManualRemoveSpawn(EndSpawnAction):
    """Bare-string default and explicit `end=("loc", MANUAL_REMOVE)`.

    Branches on `thread.run_mode`:

    - CHECK_RUN / SIM: return at once; the thread's `LabwarePlacer.left`
      frees the slot.
    - LIVE: poll `end_location.labware` every 0.5s. When the operator
      calls `labware_discharge(labware_id)`, the slot clears (discharge
      walks slot + registry), the wait returns, and the
      caller transitions the thread to COMPLETED.
    """

    def __init__(self, location: Location) -> None:
        self._location = location

    async def dispose(self, thread: LabwareThreadInstance) -> None:
        if thread.run_mode is WorkflowRunMode.LIVE:
            await self._dispose_live(thread)
        else:
            await self._dispose_sim(thread)

    async def _dispose_sim(self, thread: LabwareThreadInstance) -> None:
        """Nothing to wait for: the thread's ``LabwarePlacer.left`` frees the slot once this returns."""
        return None

    async def _dispose_live(self, thread: LabwareThreadInstance) -> None:
        """LIVE: poll `end_location.labware` for operator discharge.

        Cooperates with `thread.stop_event` so `thread.stop()` halts a
        parked operator-removal wait. The caller is
        `_handle_thread_completion`; on stop the helper returns early
        and the completion path continues to `status = COMPLETED`. An
        explicitly-stopped LIVE thread does not block on its end-slot
        beyond the next 0.5s polling tick.
        """
        while self._location.labware is not None:
            if _stopped(thread):
                return
            await asyncio.sleep(_RETRY_BACKOFF_S)


def _stopped(thread: LabwareThreadInstance) -> bool:
    """True iff the thread's cooperative-stop event has been set.

    Shared between `ManualPlaceSpawn._acquire_live` and
    `ManualRemoveSpawn._dispose_live` so the polling loop's exit
    contract stays uniform: cooperative stop drops out of the wait
    on the next polling tick. Returns False when the stop event is
    unbound (LabwareThreadInstance built outside an execution; the
    pure-unit-test path).
    """
    stop_event = thread.stop_event
    return stop_event is not None and stop_event.is_set()


def arrival_mechanism_for(thread: LabwareThreadInstance) -> ArrivalMechanism:
    """How ``thread``'s labware is going to reach its start_location.

    Mirrors `select_spawn_action`'s dispatch, so what the ledger records an
    expectation as and what later fulfils it cannot disagree.
    """
    template = thread.thread_template
    if template is not None and template.start_dispense:
        return ArrivalMechanism.DISPENSE
    return ArrivalMechanism.MANUAL_PLACE


def select_spawn_action(
    thread: LabwareThreadInstance,
    location_service: ILabwareLocationService,
    placer: LabwarePlacer,
    reservation_acquirer: PlacementReservationAcquirer | None = None,
    wait_hooks: SpawnWaitHooks | None = None,
) -> SpawnAction:
    """Return the start-side strategy that fits ``thread``'s template
    flags.

    Dispatch order:
      - `template.start_dispense` -> :class:`DispenseSpawn`
      - anything else -> :class:`ManualPlaceSpawn`, which the bare string,
        the Location and the explicit MANUAL_PLACE all reach by falling
        through

    ``reservation_acquirer`` is threaded into the chosen strategy so its sim
    placement reserves the start_location before writing the slot (the engine
    passes its ``MoveHandler``; None only in isolated unit tests).

    A reuse-bound thread reaches here when `_resolve_reuse_bind` returns None:
    a receiver replacing spent labware, or an empty site on a LIVE device.
    REUSE_EXISTING is then "anything else", so it asks the operator to place one.
    """
    template = thread.thread_template
    if template is None:
        # Defensive: a thread without a template can't declare intent.
        # Fall through to ManualPlaceSpawn, which matches the bare-string
        # default. Should not happen in factory-created threads.
        return ManualPlaceSpawn(
            thread.start_location, location_service, placer, reservation_acquirer, wait_hooks,
        )
    if template.start_dispense:
        return DispenseSpawn(
            thread.start_location, placer, reservation_acquirer, wait_hooks,
        )
    return ManualPlaceSpawn(
        thread.start_location, location_service, placer, reservation_acquirer, wait_hooks,
    )


def select_end_spawn_action(
    thread: LabwareThreadInstance,
    resting_location: Location,
    spent: bool = False,
) -> EndSpawnAction | None:
    """Return the end-side strategy that fits ``thread``'s template
    flags, or None if no end-side strategy applies.

    ``resting_location`` is the end candidate the thread actually reached;
    the manual-remove poll watches that slot, not a declared alternative.

    Dispatch order:
      - `template is None` -> None. Template-less threads have no
        author-declared end-side intent. The comment in
        `_handle_thread_completion` claiming "every author syntax
        routes to one of the two flags" is only true for
        factory-built threads with a template; synthetic test threads
        and pre-template construction paths land here. Returning None
        avoids parking these threads at `AWAITING_MANUAL_REMOVE`
        forever under LIVE (review item L4). The caller
        (`_handle_thread_completion`) treats None
        the same as `end_leave_in_place` -- skip dispose.
      - `template.end_leave_in_place` -> None (skip dispose), UNLESS
        ``spent``. Leaving labware in place is how deck-resident labware
        survives to be adopted again; labware that is used up has nothing
        left to offer the next receiver, so it goes even though the author
        declared LEAVE_IN_PLACE for the ordinary case.
      - `template.end_manual_remove` (bare-string default or explicit
        MANUAL_REMOVE) -> :class:`ManualRemoveSpawn`
    """
    template = thread.thread_template
    if template is None:
        return None
    if template.end_leave_in_place and not spent:
        return None
    return ManualRemoveSpawn(resting_location)


def _resolve_plate_source(location: Location) -> IPlateSource | None:
    """Find the IPlateSource behind a Location's resource, unwrapping
    the common LabwareStagingBridge case. Returns None if the resource
    doesn't advertise IPlateSource at any level."""
    resource = location.resource
    if isinstance(resource, IPlateSource):
        return resource
    if isinstance(resource, LabwareStagingBridge):
        device = resource.device
        if isinstance(device, IPlateSource):
            return device
    return None
