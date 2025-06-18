import asyncio
import logging
from typing import List
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.system.reservation_manager.interfaces import IReservationCollection, IThreadReservationCoordinator
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.system_map import SystemMap
from orca.workflow_models.actions.location_action import ILocationAction


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

    # async def reserve_route(self, reservation_manager: IReservationManager) -> MoveAction:
    #     for action in self._requested_move_actions:
    #         action.reservation.submit_reservation_request(reservation_manager)
        
    #     # await first route reservation completed
    #     done, pending = await asyncio.wait([asyncio.create_task(action.reservation.granted.wait()) for action in self._requested_move_actions], return_when=asyncio.FIRST_COMPLETED)

    #     # Find the first completed reservation and set it as the reservation to use
    #     for task in done:
    #         completed_reservation = next((action for action in self._requested_move_actions if action.reservation.granted.is_set()), None)
    #         if completed_reservation and not self._reservation:
    #             self._reservation = completed_reservation
    #             break
        
    #     for task in pending:
    #         task.cancel()

    #     # release and cancel any other reservations
    #     for action in self._requested_move_actions:
    #         if action != self._reservation and action.reservation.granted.is_set():
    #             action.reservation.release_reservation()
    #         elif not action.reservation.granted.is_set():
    #             action.reservation.rejected.set()
    #             action.reservation.processed.set()

    #     if self._reservation is None:
    #         raise ValueError("No route reserved")

    #     return self._reservation
    
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
                system_map: SystemMap) -> None:
        self._thread_reservation_coordinator = thread_reservation_coordinator
        self._system_map = system_map

    async def resolve_move_action(self, thread_id: str, labware: LabwareInstance, current_location: Location, target_location: Location, assigned_action: ILocationAction | None = None) -> MoveAction:
        potential_paths = self._get_potential_paths(current_location, target_location)
        potential_moves = self._get_potential_move_actions(labware, potential_paths)
        if assigned_action is not None:
            self._assign_reservation_to_moves(potential_moves, assigned_action)
        # check for any moves using the assigned_action's reservation
        for action in potential_moves:
            if action.reservation.granted.is_set():
                return action
            
        return await self._resolve_reservation_from_move_action_collection(thread_id, potential_moves)

    async def handle_deadlock(self, thread_id: str, move_action: MoveAction) -> MoveAction:
        # NOTE: Although move_action does not have a reservation and does not need to be released, 
        # even though it is set as completed, it is also deadlocked.  This may lead to confusion and may need to be changed
        # due to this, this handling may work better else where
        
        potential_paths = self._system_map.get_shortest_paths_to_deadlock_resolution(move_action.source.teachpoint_name)
        # if move_action.target is in the potential_paths, remove it
        potential_paths = [path for path in potential_paths if move_action.target.teachpoint_name not in path]
        sorted_paths = sorted(potential_paths, key=lambda path: len(path))
        potential_moves = self._get_potential_move_actions( move_action.labware, sorted_paths)
       
        return await self._resolve_reservation_from_move_action_collection(thread_id, potential_moves)


    async def _resolve_reservation_from_move_action_collection(self, thread_id: str, potential_moves: List[MoveAction]) -> MoveAction:
        reservation_request_collection = MoveActionCollectionReservationRequest(thread_id, potential_moves)
        await self._thread_reservation_coordinator.submit_reservation_request(thread_id, reservation_request_collection)
        await reservation_request_collection.processed.wait()
        if reservation_request_collection.rejected.is_set():
            await asyncio.sleep(0.2)
            orca_logger.info("Reservation request collection was rejected, retrying")
            reservation_request_collection.clear()
            return await self._resolve_reservation_from_move_action_collection(thread_id, potential_moves)
        if reservation_request_collection.deadlocked.is_set():
            reservation_request_collection.clear()
            return await self.handle_deadlock(thread_id, potential_moves[0])
        if reservation_request_collection.granted.is_set():
            return reservation_request_collection.reserved_move_action
        raise ValueError("Route reservation was not granted")
   
    def _get_potential_paths(self, current_location: Location, target_location: Location) -> List[List[str]]:
        if current_location == target_location:
            raise ValueError("Source and target locations are the same")
        potential_paths = self._system_map.get_all_shortest_any_paths(current_location.teachpoint_name, target_location.teachpoint_name)
        if potential_paths == []:
            raise ValueError("No routes found between source and target")
        return potential_paths

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
        # TODO: Fix this later - this is a temporary fix
        # use the reservation already set by the assigned action
        for move in potential_moves:
            if move.target == assigned_action.location:
                move.set_reservation(assigned_action.reservation)
                move.set_release_reservation_on_place(False)
