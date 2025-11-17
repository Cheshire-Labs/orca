import asyncio
import logging
from typing import List, Optional
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.system.reservation_manager.interfaces import IReservationCollection, IThreadReservationCoordinator
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.deadlock_manager import DeadlockStarvationRegistry
from orca.system.reservation_manager.path_scoring import PathScoringStrategy, PathScoringWeights
from orca.system.system_map import SystemMap
from orca.workflow_models.actions.location_action_interface import ILocationAction


from orca.workflow_models.actions.move_action import MoveAction
orca_logger = logging.getLogger("orca")


class MoveActionCollectionReservationRequest(IReservationCollection):
    def __init__(self, thread_id: str, requested_move_actions: List[MoveAction]):
        # ensure all the labware in each routestep is the same
        for move_action in requested_move_actions:
            if move_action.labware != requested_move_actions[0].labware:
                raise ValueError("All labware in a route must be the same")
        self._thread_id = thread_id
        self._requested_move_actions = requested_move_actions
        self._reserved_move_action: MoveAction | None = None
        self._processed = asyncio.Event()
        self._rejected = asyncio.Event()
        self._granted = asyncio.Event()
        self._deadlocked = asyncio.Event()

    @property
    def thread_id(self) -> str:
        return self._thread_id

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
    def reserved_move_action(self) -> MoveAction:
        if self._reserved_move_action is None:
            raise ValueError("No move action reserved yet")
        return self._reserved_move_action
    
    def get_reservations(self) -> List[LocationReservation]:
        return [action.reservation for action in self._requested_move_actions]

    def resolve_final_reservation(self) -> None:
        granted_reservations = [r for r in self._requested_move_actions if r.reservation.granted.is_set()]
        if len(granted_reservations) == 0:
            self._rejected.set()
            self._processed.set()
            return

        # choose the first granted reservation as the reserved move action
        self._reserved_move_action = granted_reservations[0] if len(granted_reservations) > 0 else None

        # release all other reservations
        for action in granted_reservations[1:]:
            action.reservation.release_reservation()

        self._granted.set()
        self._processed.set()

    def clear(self) -> None:
        """Clears the reservation collection, resetting all events and states."""
        if self.granted.is_set():
            # May re-examine if this should be allowed - I haven't looked into the implications yet
            raise ValueError("Cannot clear a reservation that has been granted")
        for action in self._requested_move_actions:
            action.reservation.clear()
        self._processed.clear()
        self._rejected.clear()
        self._deadlocked.clear()
        self._granted.clear()  # Clear for consistency (guard clause prevents clearing granted collections)
        


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
                scoring_weights: Optional[PathScoringWeights] = None) -> None:
        self._thread_reservation_coordinator = thread_reservation_coordinator
        self._system_map = system_map
        self._starvation_registry = starvation_registry
        self._path_scorer = PathScoringStrategy(system_map, scoring_weights)

    async def resolve_move_action(self, thread_id: str, labware: LabwareInstance, current_location: Location, target_location: Location, assigned_action: ILocationAction | None = None, previous_location: Optional[Location] = None) -> MoveAction:
        # Get ALL potential paths (not filtered by availability)
        # The reservation system will choose the first available path
        all_paths = self._system_map.get_all_shortest_any_paths(
            current_location.teachpoint_name,
            target_location.teachpoint_name
        )

        if not all_paths:
            raise ValueError(f"No routes found from {current_location.name} to {target_location.name}")

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

        if assigned_action is not None:
            self._assign_reservation_to_moves(potential_moves, assigned_action)
        # check for any moves using the assigned_action's reservation
        for action in potential_moves:
            if action.reservation.granted.is_set():
                return action

        return await self._resolve_reservation_from_move_action_collection(thread_id, potential_moves)

    async def handle_deadlock(self, thread_id: str, move_action: MoveAction, previous_location: Optional[Location] = None) -> MoveAction:
        # NOTE: Although move_action does not have a reservation and does not need to be released,
        # even though it is set as completed, it is also deadlocked.  This may lead to confusion and may need to be changed
        # due to this, this handling may work better else where

        # Get all paths to parking pads
        all_deadlock_paths = self._system_map.get_shortest_paths_to_deadlock_resolution(
            move_action.source.teachpoint_name
        )

        if not all_deadlock_paths:
            raise RuntimeError(f"Thread {thread_id} - No parking pads available for deadlock resolution")

        # Score and select best parking pad using PathScoringStrategy
        # This considers: path length, starvation score, backtracking, and dead-end detection
        starvation_score = self._starvation_registry.get_starvation_score(thread_id)
        best_path, score = self._path_scorer.select_best_path(
            all_deadlock_paths,
            thread_id,
            starvation_score,
            previous_location,
            original_target=move_action.target,  # Provide original target for dead-end detection
            blocked_location=move_action.target  # Filter out paths containing blocked target
        )

        orca_logger.info(
            f"Thread {thread_id} - Deadlock resolution: moving to parking pad "
            f"{best_path[-1]} (starvation score: {starvation_score}, total score: {score.total_score:.2f})"
        )

        # Create move actions for the best path (only first hop)
        potential_moves = self._get_potential_move_actions(move_action.labware, [best_path])
        return await self._resolve_reservation_from_move_action_collection(thread_id, potential_moves)


    async def _resolve_reservation_from_move_action_collection(
        self,
        thread_id: str,
        potential_moves: List[MoveAction]
    ) -> MoveAction:
        """
        Resolve a reservation from a collection of potential move actions.
        Uses iterative retry with exponential backoff (no recursion).

        Args:
            thread_id: Thread requesting the reservation
            potential_moves: List of potential move actions to try

        Returns:
            The granted MoveAction

        Raises:
            RuntimeError: If max retries exceeded (indicates persistent deadlock or system issue)
            ValueError: If reservation in unexpected state
        """
        backoff = 0.05  # Start with 50ms
        max_backoff = 1.0  # Cap at 1 second
        retry_count = 0
        max_retries = 100  # Safety limit (should rarely hit with event-driven system)

        while True:
            if retry_count >= max_retries:
                raise RuntimeError(
                    f"Thread {thread_id} exceeded maximum reservation retries ({max_retries}). "
                    f"This likely indicates a persistent deadlock or system issue."
                )

            collection = MoveActionCollectionReservationRequest(thread_id, potential_moves)
            await self._thread_reservation_coordinator.submit_reservation_request(thread_id, collection)
            await collection.processed.wait()

            if collection.granted.is_set():
                return collection.reserved_move_action

            elif collection.deadlocked.is_set():
                collection.clear()
                return await self.handle_deadlock(thread_id, potential_moves[0])

            elif collection.rejected.is_set():
                # Exponential backoff
                retry_count += 1
                orca_logger.debug(
                    f"Thread {thread_id} - Reservation rejected, retry {retry_count}/{max_retries} "
                    f"after {backoff:.2f}s backoff"
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, max_backoff)
                collection.clear()
                # Loop continues - no recursion!

            else:
                raise ValueError(
                    f"Thread {thread_id} - Reservation collection not granted, rejected, or deadlocked. "
                    f"This should never happen."
                )
   
    def _get_potential_move_actions(self, labware: LabwareInstance, potential_paths: List[List[str]]) -> List[MoveAction]:
        potential_actions: List[MoveAction] = []
        for path in potential_paths:
            source_location = self._system_map.get_location(path[0])
            target = self._system_map.get_location(path[1])
            transporter = self._system_map.get_transporter_between(source_location.name, target.name)
            action = MoveAction(labware, source_location, target, transporter)
            potential_actions.append(action)
        return potential_actions
    
    def _assign_reservation_to_moves(self, potential_moves: List[MoveAction], assigned_action: ILocationAction) -> None:
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
