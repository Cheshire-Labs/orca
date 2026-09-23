import asyncio
import logging
from typing import AbstractSet, Callable, List, Optional
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.config import ReservationConfig
from orca.system.reservation_manager.errors import (
    MoveAbandonedError,
    UnresolvableDeadlockContext,
    UnresolvableDeadlockError,
)
from orca.system.reservation_manager.interfaces import IReservationCollection, IThreadReservationCoordinator
from orca.system.reservation_manager.location_reservation import (
    LocationReservation,
    ReservationPriority,
)
from orca.system.reservation_manager.deadlock_manager import DeadlockStarvationRegistry
from orca.system.reservation_manager.deadlock_recovery import DeadlockRecoveryStrategy
from orca.system.reservation_manager.path_scoring import PathScoringStrategy, PathScoringWeights
from orca.system.system_map import SystemMap
from orca.workflow_models.actions.location_action import LocationAction


from orca.workflow_models.actions.move_action import MoveAction
orca_logger = logging.getLogger("orca")


class MoveActionCollectionReservationRequest(IReservationCollection):
    def __init__(self, thread_id: str, requested_move_actions: List[MoveAction]):
        # ensure all the labware in each routestep is the same
        for move_action in requested_move_actions:
            if move_action.labware != requested_move_actions[0].labware:
                raise ValueError("All labware in a route must be the same")
        self._dedupe_shared_positions(requested_move_actions)
        self._thread_id = thread_id
        self._requested_move_actions = requested_move_actions
        self._reserved_move_action: MoveAction | None = None
        self._processed = asyncio.Event()
        self._rejected = asyncio.Event()
        self._granted = asyncio.Event()
        self._deadlocked = asyncio.Event()
        self._unresolvable_deadlock = asyncio.Event()
        self._unresolvable_deadlock_context: UnresolvableDeadlockContext | None = None

    @property
    def thread_id(self) -> str:
        return self._thread_id

    @property
    def resets_starvation_on_grant(self) -> bool:
        return False

    @property
    def processed(self) -> asyncio.Event:
        return self._processed

    @property
    def rejected(self) -> asyncio.Event:
        return self._rejected

    @property
    def granted(self) -> asyncio.Event:
        return self._granted

    @property
    def deadlocked(self) -> asyncio.Event:
        return self._deadlocked

    @property
    def unresolvable_deadlock(self) -> asyncio.Event:
        return self._unresolvable_deadlock

    @property
    def unresolvable_deadlock_context(self) -> UnresolvableDeadlockContext | None:
        return self._unresolvable_deadlock_context

    def set_unresolvable_deadlock(self, context: UnresolvableDeadlockContext) -> None:
        self._unresolvable_deadlock_context = context
        self._unresolvable_deadlock.set()

    @property
    def reserved_move_action(self) -> MoveAction:
        if self._reserved_move_action is None:
            raise ValueError("No move action reserved yet")
        return self._reserved_move_action
    
    def get_reservations(self) -> List[LocationReservation]:
        reservations: List[LocationReservation] = []
        for action in self._requested_move_actions:
            reservations.append(action.reservation)
            reservations.extend(action.owned_onward_reservations)
        return reservations

    @staticmethod
    def _holds(reservation: LocationReservation) -> bool:
        # A displaced reservation still reads granted but no longer owns the
        # manager's entry; crowning one double-books the location.
        return reservation.granted.is_set() and not reservation.is_displaced

    @classmethod
    def _entry_granted(cls, action: MoveAction, reservation: LocationReservation) -> bool:
        if action.is_substituted_onward(reservation):
            # Action-owned hold: granted is the action's promise of the
            # position; displacement is its own lifecycle, not ours.
            return reservation.granted.is_set()
        return cls._holds(reservation)

    @classmethod
    def _fully_granted(cls, action: MoveAction) -> bool:
        if not cls._holds(action.reservation):
            return False
        if not all(
            cls._entry_granted(action, r) for r in action.onward_seat_reservations
        ):
            return False
        terminals = action.terminal_reservations
        if not terminals:
            return True
        return any(cls._entry_granted(action, r) for r in terminals)

    @staticmethod
    def _dedupe_shared_positions(moves: List[MoveAction]) -> None:
        """One reservation object per position across the collection.

        Sibling candidate moves sharing a position (two bridges to one
        terminal) must reference ONE object: separate same-thread requests
        displace each other at attempt time, and the displaced free route
        then rejects the whole collection every retry, forever."""
        canonical: dict[str, LocationReservation] = {}
        for move in moves:
            target_existing = canonical.get(move.target.position_id)
            if target_existing is None:
                canonical[move.target.position_id] = move.reservation
            elif move.reservation is not target_existing:
                move.set_reservation(target_existing)
            for entry in [*move.onward_seat_reservations, *move.terminal_reservations]:
                position = entry.requested_location.position_id
                existing = canonical.get(position)
                if existing is None:
                    canonical[position] = entry
                elif entry is not existing:
                    move.share_onward_reservation(position, existing)

    @classmethod
    def _release_holds(
        cls, action: MoveAction, exempt: AbstractSet[str] = frozenset()
    ) -> None:
        for reservation in [action.reservation, *action.owned_onward_reservations]:
            if reservation.id in exempt:
                continue
            if cls._holds(reservation):
                reservation.release_and_ungrant()

    @classmethod
    def _kept_terminal(cls, action: MoveAction) -> LocationReservation | None:
        """The one terminal the crossing keeps. An action-owned (substituted)
        grant wins: the action holds that position regardless, so keeping a
        fresh grant beside it would hold two terminals."""
        granted = [
            r for r in action.terminal_reservations if cls._entry_granted(action, r)
        ]
        for reservation in granted:
            if action.is_substituted_onward(reservation):
                return reservation
        return granted[0] if granted else None

    @classmethod
    def _release_surplus_terminals(cls, action: MoveAction) -> None:
        # The crossing needs ONE resting terminal; surplus grants would
        # starve the sibling candidates for every other thread.
        kept = cls._kept_terminal(action)
        for reservation in action.terminal_reservations:
            if reservation is kept or action.is_substituted_onward(reservation):
                continue
            if cls._holds(reservation):
                reservation.release_and_ungrant()

    def resolve_final_reservation(self) -> None:
        # Whole corridor run or nothing: boarding a single-carriage position
        # without its onward hop strands the labware on the carriage.
        fully_granted = [
            action for action in self._requested_move_actions
            if self._fully_granted(action)
        ]
        if len(fully_granted) == 0:
            for action in self._requested_move_actions:
                self._release_holds(action)
            self._rejected.set()
            self._processed.set()
            return

        self._reserved_move_action = fully_granted[0]
        crowned = self._reserved_move_action

        # A shared object the crowned route rides may be OWNED by a losing
        # sibling; releasing it with the loser would tear out the winner's hold.
        kept_ids = {crowned.reservation.id}
        kept_ids.update(r.id for r in crowned.onward_seat_reservations)
        kept_terminal = self._kept_terminal(crowned)
        if kept_terminal is not None:
            kept_ids.add(kept_terminal.id)

        for action in self._requested_move_actions:
            if action is not crowned:
                self._release_holds(action, exempt=kept_ids)
        self._release_surplus_terminals(crowned)

        self._granted.set()
        self._processed.set()

    def clear(self) -> None:
        """Reset processed/rejected/deadlocked for retry of a non-granted collection.

        Invariant: a granted MoveActionCollectionReservationRequest holds a
        real route reservation that the owning thread will execute against.
        Clearing it would orphan the underlying location locks. The retry
        loop in ``MoveHandler.attempt_to_reserve_move`` only calls clear()
        on the deadlocked or rejected branches, so this guard is defensive:
        a granted collection here means a caller violated the protocol.
        """
        if self.granted.is_set():
            raise RuntimeError(
                "MoveActionCollectionReservationRequest.clear() invariant "
                "violated: cannot clear a granted collection; release the "
                "reserved move action's underlying locks first.",
            )
        for action in self._requested_move_actions:
            action.reservation.clear()
            for reservation in action.owned_onward_reservations:
                reservation.clear()
        self._processed.clear()
        self._rejected.clear()
        self._deadlocked.clear()
        self._granted.clear()  # Clear for consistency (guard clause prevents clearing granted collections)
        self._unresolvable_deadlock.clear()
        self._unresolvable_deadlock_context = None
        


    def __str__(self) -> str:
        output =  f"Route Reservation: Requested Route Steps: {self._requested_move_actions}"
        # print out each requested route step on a separate line
        for route_step in self._requested_move_actions:
            output += f"\n - {route_step}"
        if self._reserved_move_action:
            output += f"\n - Reserved Route Step: {self._reserved_move_action.reservation.reserved_location}"
        else:
            output += "\n - Not yet reserved"
        return output


class MoveHandler:
    def __init__(self,
                thread_reservation_coordinator: IThreadReservationCoordinator,
                system_map: SystemMap,
                starvation_registry: DeadlockStarvationRegistry,
                reservation_config: ReservationConfig | None = None,
                scoring_weights: Optional[PathScoringWeights] = None) -> None:
        self._thread_reservation_coordinator = thread_reservation_coordinator
        self._system_map = system_map
        self._starvation_registry = starvation_registry
        self._reservation_config = reservation_config or ReservationConfig()
        self._path_scorer = PathScoringStrategy(system_map, scoring_weights)
        self._recovery_strategy = DeadlockRecoveryStrategy()
        # Live corridor holds per thread: the seat chain + terminal
        # a boarding grant carried, until consumed by crossing legs or swept.
        self._corridor_holds: dict[str, List[LocationReservation]] = {}

    @property
    def system_map(self) -> SystemMap:
        return self._system_map

    def get_location(self, name: str) -> Location:
        return self._system_map.get_location(name)

    def resolve_journey_location(self, name: str) -> Location:
        """A taught name as a MOVE target: device names give their site, never
        the off-graph mutex. Same resolution thread `start=`/`end=` uses."""
        return self._system_map.resolve_journey_location(name)

    def _live_holds(self, thread_id: str) -> List[LocationReservation]:
        return [
            r for r in self._corridor_holds.get(thread_id, [])
            if r.granted.is_set() and not r.is_displaced
        ]

    def _record_corridor_holds(self, thread_id: str, move: MoveAction) -> None:
        # Entries whose holds all died (thread failed or aborted mid-route)
        # would otherwise sit in the dict for the runtime's lifetime.
        for stale_thread in [
            t for t in self._corridor_holds if t != thread_id and not self._live_holds(t)
        ]:
            self._corridor_holds.pop(stale_thread)
        # Shared entries ride a sibling-owned object; the crossing still
        # depends on them, so they are tracked (and swept) like owned ones.
        holds = [
            r for r in [*move.owned_onward_reservations, *move.shared_onward_reservations]
            if r.granted.is_set() and not r.is_displaced
        ]
        # A route change can DROP a prior hold instead of consuming it by
        # displacement; forgetting its grant without releasing squats the position.
        kept_ids = {r.id for r in holds}
        for stale in self._live_holds(thread_id):
            if stale.id in kept_ids or stale is move.reservation:
                continue
            try:
                self._thread_reservation_coordinator.cancel_reservation_by_id(stale.id)
            except KeyError:
                pass
        if holds:
            self._corridor_holds[thread_id] = holds
        else:
            self._corridor_holds.pop(thread_id, None)

    def _sweep_stale_corridor_holds(self, thread_id: str, current_location: Location) -> None:
        """A hold that outlived its crossing is released once the labware is
        at rest. Consumption is the normal end (a crossing leg displaces the
        hold as ownership hands over); this sweep only catches route changes,
        e.g. a mutation retargeting the thread away from its insured landing."""
        held = self._live_holds(thread_id)
        if not held:
            self._corridor_holds.pop(thread_id, None)
            return
        if self._system_map.is_carriage_position(current_location.position_id):
            return
        for reservation in held:
            try:
                self._thread_reservation_coordinator.cancel_reservation_by_id(reservation.id)
            except KeyError:
                pass
        self._corridor_holds.pop(thread_id, None)

    @staticmethod
    def _pre_granted(action: MoveAction) -> bool:
        """An assigned-reservation move short-circuits the collection only when
        its corridor run (if any) is granted too, or it re-opens the stranded-
        on-carriage hole that the corridor holds are there to close."""
        if not (action.reservation.granted.is_set() and not action.reservation.is_displaced):
            return False
        if not all(r.granted.is_set() for r in action.onward_seat_reservations):
            return False
        terminals = action.terminal_reservations
        if not terminals:
            return True
        return any(r.granted.is_set() for r in terminals)

    async def resolve_move_action(self, thread_id: str, labware: LabwareInstance, current_location: Location, targets: list[Location], assigned_action: LocationAction | None = None, previous_location: Optional[Location] = None, escape_on_arrival: bool = True, abandon_when: Callable[[], bool] | None = None) -> MoveAction:
        self._sweep_stale_corridor_holds(thread_id, current_location)
        # Interchangeable candidates: paths to EVERY target
        # feed one collection; the reservation grant picks the free one.
        all_paths: List[List[str]] = []
        for target in targets:
            all_paths.extend(self._system_map.get_all_shortest_any_paths(
                current_location.position_id,
                target.position_id,
            ))

        # Transit ban: drop paths that corridor through a foreign
        # device's deck sites; per-path service set = that path's own endpoints.
        candidate_count = len(all_paths)
        all_paths = [
            path for path in all_paths
            if self._system_map.is_path_admissible(path)
        ]

        if not all_paths:
            names = ", ".join(t.name for t in targets)
            if candidate_count:
                raise ValueError(
                    f"No routes found from {current_location.name} to {names}: "
                    f"{candidate_count} candidate path(s) rejected by the deck "
                    f"transit ban (a deck site is never a corridor between "
                    f"other devices)."
                )
            raise ValueError(f"No routes found from {current_location.name} to {names}")

        # Score and sort paths to prioritize better options
        starvation_score = self._starvation_registry.get_starvation_score(thread_id)
        scored_paths = self._path_scorer.score_paths(
            all_paths,
            thread_id,
            starvation_score,
            previous_location,
            original_target=None  # Not deadlock resolution
        )

        # Extract paths ordered by score (best first)
        sorted_paths = [sp.path for sp in scored_paths]

        # Create move actions for ALL paths - reservation system picks first available
        potential_moves = self._get_potential_move_actions(labware, sorted_paths)

        # A live corridor hold is consumed by crowning its position first;
        # any other crown would orphan the hold (nothing releases it later).
        held_positions = {
            r.requested_location.position_id for r in self._live_holds(thread_id)
        }
        if held_positions:
            potential_moves.sort(key=lambda m: m.target.position_id not in held_positions)

        if assigned_action is not None:
            self._assign_reservation_to_moves(potential_moves, assigned_action)
        # check for any moves using the assigned_action's reservation
        for action in potential_moves:
            if self._pre_granted(action):
                if escape_on_arrival:
                    self._mark_episode_escaped(thread_id)
                self._record_corridor_holds(thread_id, action)
                return action

        result = await self._resolve_reservation_from_move_action_collection(
            thread_id, potential_moves, abandon_when=abandon_when,
        )
        self._record_corridor_holds(thread_id, result)
        # A granted intermediate hop (e.g. the boomerang return to a just-vacated
        # spot) is not an escape; only arrival at a requested target ends the episode.
        if escape_on_arrival and result.target in targets:
            self._mark_episode_escaped(thread_id)
        return result

    def _mark_episode_escaped(self, thread_id: str) -> None:
        """Arrival at a true move target ends the thread's blocked episode:
        clear the pad cooldown and the starvation sacrifice-debt together.
        Callers whose targets are a YIELD destination (acquisition-yield
        vacate) pass ``escape_on_arrival=False``: a yield never pays down
        the debt, or the yielder is re-crowned victim every lap."""
        self._recovery_strategy.clear_cooldown(thread_id)
        self._starvation_registry.reset_starvation_score(thread_id)

    async def acquire_placement_reservation(
        self, thread_id: str, labware: LabwareInstance, location: Location
    ) -> LocationReservation:
        """Blocking-acquire an exclusive reservation on ``location`` for a spawn
        placing a fresh plate, held only until the caller releases it.

        Symmetric with how a move reserves its destination: a spawn must not drop
        a plate onto a location another thread has reserved and is mid-transit
        toward (the reserving move has picked its plate, so the slot is
        momentarily empty). Without this the spawn sees an empty slot and places,
        and the reserving plate collides on arrival. The caller releases right
        after ``place_labware`` succeeds; physical occupancy then guards the
        resident plate exactly as before. Wakes when a RESERVATION is released
        and re-checks both gates; a wait blocked on physical occupancy alone is
        retried on ``retry_interval`` (see the two-gate model on
        ``wait_for_location_release``). Bounded by ``move_reservation_timeout``
        (None = wait indefinitely; placements, like moves, can wait hours for a
        device to clear).
        """
        return await self._await_location_reservation(
            thread_id, labware, location, "location"
        )

    async def try_acquire_placement_reservation(
        self, thread_id: str, labware: LabwareInstance, location: Location
    ) -> LocationReservation | None:
        """One attempt at the same claim, or None when somebody else holds it.

        For a spawn that cannot sit inside a blocking acquire because the thing
        it is really waiting for is a person: a LIVE manual place holds this
        claim so the engine cannot promise the slot to another thread while an
        operator is being asked to fill it, and re-tries it on the same tick it
        checks whether the labware arrived. Blocking would make an operator who
        placed the plate wait out a timeout that has nothing left to wait for.

        Held at ``AWAITING_OPERATOR``, the bottom tier: the plate this claim
        is holding the spot for does not exist yet, so a thread carrying a
        real plate that needs this location takes it and this claim is
        displaced. The caller sees the displacement and asks again.
        """
        request = LocationReservation(
            location, labware, priority=ReservationPriority.AWAITING_OPERATOR,
        )
        granted = await self._thread_reservation_coordinator.try_reserve_location(
            thread_id, location.position_id, request
        )
        return request if granted else None

    async def hold_the_source(
        self, thread_id: str, labware: LabwareInstance, source: Location,
    ) -> LocationReservation | None:
        """Take ``source`` while its plate is still standing in it, or None.

        A mover whose pick actuates nothing carries the plate with one driver
        call at place time, so the plate is on its slot for the whole move while
        the record already has it in the jaws. Without this the slot reads free
        for that whole window and the next thread is granted it.

        Single-shot, and None rather than a wait when somebody else holds it:
        the mover needs nothing else while it carries, so it stays out of the
        wait-for graph, and a slot already held is left as it is.
        """
        request = LocationReservation(source, labware)
        granted = await self._thread_reservation_coordinator.try_reserve_location(
            thread_id, source.position_id, request
        )
        return request if granted else None

    async def _await_location_reservation(
        self,
        thread_id: str,
        labware: LabwareInstance,
        location: Location,
        descriptor: str,
    ) -> LocationReservation:
        position_id = location.position_id
        timeout = self._reservation_config.move_reservation_timeout
        retry_interval = self._reservation_config.retry_interval
        start_time = asyncio.get_event_loop().time()
        while True:
            release_snapshot = self._thread_reservation_coordinator.release_snapshot([position_id])
            request = LocationReservation(location, labware)
            granted = await self._thread_reservation_coordinator.try_reserve_location(
                thread_id, position_id, request
            )
            if granted:
                return request
            if timeout is not None and asyncio.get_event_loop().time() - start_time >= timeout:
                raise RuntimeError(
                    f"Thread {thread_id} timed out waiting for {descriptor} "
                    f"{position_id} ({timeout}s elapsed)."
                )
            await self._thread_reservation_coordinator.wait_for_location_release(
                release_snapshot, retry_interval
            )

    async def handle_deadlock(self, thread_id: str, move_action: MoveAction, previous_location: Optional[Location] = None, abandon_when: Callable[[], bool] | None = None) -> MoveAction:
        potential_moves = self._build_parking_move_collection(
            thread_id, move_action, previous_location
        )
        result = await self._resolve_reservation_from_move_action_collection(
            thread_id, potential_moves, abandon_when=abandon_when,
        )
        self._record_corridor_holds(thread_id, result)
        return result

    def release_stale_corridor_holds(
        self, thread_id: str, current_location: Location,
    ) -> None:
        """Give up crossing holds this thread no longer needs.

        Every route change reaches this through the next resolve; a thread that
        stops moving because its labware is already where it was headed has no
        next resolve, and its holds would sit for the life of the runtime.
        """
        self._sweep_stale_corridor_holds(thread_id, current_location)

    def _vacate_moves(
        self, thread_id: str, potential_moves: List[MoveAction]
    ) -> List[MoveAction]:
        """Fallback moves to deadlock-resolution pads for a starved move whose
        labware sits on a device-owned working site.

        The device is a shared resource and the pad is idle parking, so once
        patience runs out the plate offers to clear the deck. Real targets
        stay ahead of these in the collection and win whenever they free; a
        pad grant is not an escape (the thread re-resolves from the pad).
        """
        source = potential_moves[0].source
        if source.owner_mutex_id is None:
            return []
        if self._system_map.is_deadlock_resolution_location(source.position_id):
            return []
        paths = self._system_map.get_shortest_paths_to_deadlock_resolution(
            source.position_id
        )
        # Episode cooldown (same anti-oscillation rule as deadlock parking):
        # a pad this episode already vacated to is not offered again.
        visited = self._recovery_strategy.get_cooldown(thread_id)
        paths = [
            p for p in paths
            if self._park_completes(p) and p[1] not in visited and p[-1] not in visited
        ]
        if not paths:
            return []
        taken_hops = {m.target.position_id for m in potential_moves}
        vacates = self._get_potential_move_actions(
            potential_moves[0].labware, paths
        )
        vacates = [m for m in vacates if m.target.position_id not in taken_hops]
        if vacates:
            orca_logger.info(
                f"Thread {thread_id} - Move starved on device site "
                f"{source.position_id}: widening with vacate pads "
                f"{[m.target.position_id for m in vacates]}"
            )
        return vacates

    def _park_completes(self, path: List[str]) -> bool:
        """Does ONE resolved move actually land this plate on the pad?

        Moves resolve a hop at a time and re-resolve toward the REAL target
        afterwards, so a park the first move does not finish is never finished:
        the plate stops on the transit position in between and the next
        resolution walks it straight back off, squatting a crossing every other
        thread needs. A bridge boarding still counts -- its whole corridor is
        reserved together, so the plate comes to rest at the terminal.
        """
        if len(path) < 2:
            return False
        onward = self._system_map.boarding_onward_positions(path)
        resting = onward[-1] if onward else path[1]
        return resting == path[-1]

    def _widen_with_parking(
        self, thread_id: str, potential_moves: List[MoveAction]
    ) -> List[MoveAction]:
        """Offer a resolution pad ALONGSIDE the real targets, never instead.

        Parking is the yield the detector asked for, so it belongs in the
        collection as one more candidate: the pick-one grant takes the real
        target whenever it frees and the pad only while it stays blocked, the
        same shape ``_vacate_moves`` uses. Replacing the collection with a
        park-only one strands the thread the moment no pad can be granted --
        it stops asking for its destination, so no later release reaches it,
        and the rebuilt park keeps flipping between pads forever.
        """
        try:
            parking = self._build_parking_move_collection(thread_id, potential_moves[0])
        except RuntimeError:
            # No pad the route can reach. There is nothing to yield with, but
            # the destination is still worth asking for -- failing the move on
            # a transient flag is the same lost target by another door.
            return potential_moves
        taken = {move.target.position_id for move in potential_moves}
        return potential_moves + [
            move for move in parking if move.target.position_id not in taken
        ]

    def _build_parking_move_collection(
        self,
        thread_id: str,
        move_action: MoveAction,
        previous_location: Optional[Location] = None,
    ) -> List[MoveAction]:
        all_deadlock_paths = [
            path
            for path in self._system_map.get_shortest_paths_to_deadlock_resolution(
                move_action.source.position_id
            )
            if self._park_completes(path)
        ]

        if not all_deadlock_paths:
            raise RuntimeError(f"Thread {thread_id} - No parking pads available for deadlock resolution")

        # Build avoidance set: pads reserved by other threads + pads this thread already visited
        reserved_by_others = self._thread_reservation_coordinator.get_reserved_position_ids(
            exclude_thread_id=thread_id
        )
        # A plate deadlock-parking left on a pad holds it with occupancy only,
        # no reservation; without this the picker picks a pad that never grants.
        for path in all_deadlock_paths:
            occupant = self._system_map.get_location(path[-1]).labware
            if occupant is not None and occupant.id != move_action.labware.id:
                reserved_by_others.add(path[-1])
        avoid = self._recovery_strategy.get_locations_to_avoid(thread_id, reserved_by_others)

        starvation_score = self._starvation_registry.get_starvation_score(thread_id)
        # EVERY reachable pad, best-scored first. The collection is pick-one, so
        # offering only the best pad bets the escape on that one freeing; when a
        # sibling frees instead, nothing re-picks and the yield never happens.
        scored = self._path_scorer.score_paths(
            all_deadlock_paths,
            thread_id,
            starvation_score,
            previous_location,
            original_target=move_action.target,
            blocked_location=move_action.target,
            occupied_locations=avoid,
        )
        ordered = [candidate.path for candidate in scored]

        chosen_pad = ordered[0][-1]
        self._recovery_strategy.record_visit(thread_id, chosen_pad)

        orca_logger.info(
            f"Thread {thread_id} - Deadlock resolution: offering parking pads "
            f"{[path[-1] for path in ordered]} (starvation score: "
            f"{starvation_score}, best score: {scored[0].total_score:.2f})"
        )

        return self._get_potential_move_actions(move_action.labware, ordered)


    async def _resolve_reservation_from_move_action_collection(
        self,
        thread_id: str,
        potential_moves: List[MoveAction],
        abandon_when: Callable[[], bool] | None = None,
    ) -> MoveAction:
        """Resolve a reservation from a collection of potential move actions.

        On rejection, retries the whole collection after sleeping
        ``retry_interval`` -- a bounded poll that also gives the blocking thread
        settle time so the re-attempt does not immediately re-form the same
        deadlock cycle -- until granted or ``move_reservation_timeout`` elapses,
        then raises RuntimeError, which propagates to the move error handler and
        pauses the thread for operator intervention. Deadlock and rejection both
        iterate in-loop; the previous deadlock branch recursed through
        handle_deadlock, growing the stack one frame per event and blowing the
        recursion limit under heavy concurrent contention.
        """
        timeout = self._reservation_config.move_reservation_timeout
        retry_interval = self._reservation_config.retry_interval
        start_time = asyncio.get_event_loop().time()
        vacate_widened = False
        vacate_targets: set[str] = set()
        park_widened = False

        while True:
            collection = MoveActionCollectionReservationRequest(thread_id, potential_moves)
            await self._thread_reservation_coordinator.submit_reservation_request(thread_id, collection)
            await collection.processed.wait()

            # S3 Round 1: detector declared an unresolvable deadlock (e.g.,
            # the target is held by an `immovable=True` thread). Raise the
            # typed error to break out of the retry loop -- no amount of
            # waiting will free the blocker. Mirrors the action-resolver
            # path in ResourcePoolResolver.resolve_action_location so move
            # paths fail fast on the same condition.
            if collection.unresolvable_deadlock.is_set():
                context = collection.unresolvable_deadlock_context
                if context is None:
                    raise ValueError(
                        "unresolvable_deadlock event set without context"
                    )
                raise UnresolvableDeadlockError(context)

            if collection.granted.is_set():
                result = collection.reserved_move_action
                # Landing on a vacate pad starts its cooldown for this episode;
                # a real-target arrival clears it (_mark_episode_escaped).
                if result.target.position_id in vacate_targets:
                    self._recovery_strategy.record_visit(
                        thread_id, result.target.position_id
                    )
                return result

            elif collection.deadlocked.is_set():
                collection.clear()
                # Widen once per episode: re-picking a pad on every flag is
                # what made two pads alternate forever, and the offer already
                # in the collection is retried by the resubmission below.
                if not park_widened:
                    park_widened = True
                    potential_moves = self._widen_with_parking(
                        thread_id, potential_moves
                    )

            elif collection.rejected.is_set():
                collection.clear()
                # Checked only on a rejected cycle: the collection holds
                # nothing here, so withdrawing leaves no partial grants.
                if abandon_when is not None and abandon_when():
                    raise MoveAbandonedError(
                        f"Thread {thread_id} withdrew its move request "
                        f"(targets: {', '.join(m.target.name for m in potential_moves)})"
                    )
                elapsed = asyncio.get_event_loop().time() - start_time
                if timeout is not None and elapsed >= timeout:
                    target_names = ", ".join(m.target.name for m in potential_moves)
                    raise RuntimeError(
                        f"Thread {thread_id} timed out waiting for move reservation "
                        f"(targets: {target_names}, {timeout}s elapsed)."
                    )
                if (
                    not vacate_widened
                    and elapsed >= self._reservation_config.site_vacate_patience
                ):
                    vacate_widened = True
                    vacates = self._vacate_moves(thread_id, potential_moves)
                    vacate_targets.update(m.target.position_id for m in vacates)
                    potential_moves = potential_moves + vacates
                await asyncio.sleep(retry_interval)

            else:
                raise ValueError(
                    f"Thread {thread_id} - Reservation collection not granted, rejected, or deadlocked. "
                    f"This should never happen."
                )
   
    def _get_potential_move_actions(self, labware: LabwareInstance, potential_paths: List[List[str]]) -> List[MoveAction]:
        # One move per FIRST HOP: two moves on one hop means two reservations for
        # one location in a collection, where the loser's release frees the winner.
        # One move per FIRST HOP (duplicate hops double-book one location); a
        # hop's corridor merges same-chain terminals as any-of.
        hop_order: List[str] = []
        seats_by_hop: dict[str, List[str]] = {}
        terminals_by_hop: dict[str, List[str]] = {}
        for path in potential_paths:
            hop = path[1]
            run = self._system_map.boarding_onward_positions(path)
            seats, terminals = (run[:-1], [run[-1]]) if run else ([], [])
            if hop not in seats_by_hop:
                hop_order.append(hop)
                seats_by_hop[hop] = seats
                terminals_by_hop[hop] = list(terminals)
                continue
            if seats == seats_by_hop[hop]:
                for terminal in terminals:
                    if terminal not in terminals_by_hop[hop]:
                        terminals_by_hop[hop].append(terminal)

        source_location = self._system_map.get_location(potential_paths[0][0])
        potential_actions: List[MoveAction] = []
        for hop in hop_order:
            target = self._system_map.get_location(hop)
            transporter = self._system_map.get_transporter_between(source_location.name, target.name)
            action = MoveAction(
                labware, source_location, target, transporter,
                [self._system_map.get_location(p) for p in seats_by_hop[hop]],
                [self._system_map.get_location(p) for p in terminals_by_hop[hop]],
            )
            potential_actions.append(action)
        return potential_actions
    
    def _assign_reservation_to_moves(self, potential_moves: List[MoveAction], assigned_action: LocationAction) -> None:
        """
        Assigns an existing reservation from an action to matching move actions.

        This is used when an action has already obtained a reservation for its location,
        and we're creating move actions to reach that location. We reuse the existing
        reservation rather than requesting a new one.

        Args:
            potential_moves: List of move actions we're considering
            assigned_action: Action that already has a reservation for its location
        """
        for move in potential_moves:
            if move.target == assigned_action.location:
                move.set_reservation(assigned_action.reservation)
                move.set_release_reservation_on_place(False)  # Action owns the reservation
                orca_logger.debug(
                    f"Reusing reservation {assigned_action.reservation.id} "
                    f"for move to {move.target.name}"
                )
            # Corridor runs ending at the action's location reuse its hold too:
            # a fresh request there fights the group owner's reservation.
            move.substitute_onward_reservation(
                assigned_action.location.position_id, assigned_action.reservation
            )
