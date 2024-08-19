from orca.resource_models.labware import Labware
from orca.resource_models.location import Location
from orca.system.labware_registry_interfaces import ILabwareRegistry
from orca.system.reservation_manager import IReservationManager
from orca.system.system_map import SystemMap
from orca.workflow_models.action import LocationAction, MoveAction
from orca.workflow_models.route import Route

import asyncio
from typing import List

class MoveActionCollectionReservationRequest:
    def __init__(self, requested_move_actions: List[MoveAction]):
        # ensure all the labware in each routestep is the same
        for move_action in requested_move_actions:
            if move_action.labware != requested_move_actions[0].labware:
                raise ValueError("All labware in a route must be the same")
        self._requested_move_actions = requested_move_actions
        self._reservation: MoveAction | None = None

    async def reserve_route(self, reservation_manager: IReservationManager) -> MoveAction:
        for action in self._requested_move_actions:
            action.reservation.request_reservation(reservation_manager)
            if action.reservation.completed.is_set():
                break
        
        # await first route reservation completed
        done, pending = await asyncio.wait([asyncio.create_task(action.reservation.completed.wait()) for action in self._requested_move_actions], return_when=asyncio.FIRST_COMPLETED)

        # Find the first completed reservation and set it as the reservation to use
        for task in done:
            completed_reservation = next((action for action in self._requested_move_actions if action.reservation.completed.is_set()), None)
            if completed_reservation and not self._reservation:
                self._reservation = completed_reservation
                break
        
        for task in pending:
            task.cancel()

        # release and cancel any other reservations
        for action in self._requested_move_actions:
            if action != self._reservation and action.reservation.completed.is_set():
                action.reservation.release_reservation()
            elif not action.reservation.completed.is_set():
                action.reservation.cancelled.set()

        if self._reservation is None:
            raise ValueError("No route reserved")

        return self._reservation


    def __str__(self) -> str:
        output =  f"Route Reservation: Requested Route Steps: {self._requested_move_actions}"
        # print out each requested route step on a separate line
        for route_step in self._requested_move_actions:
            output += f"\n - {route_step}"
        if self._reservation:
            output += f"\n - Reserved Route Step: {self._reservation.reservation.reserved_location}"
        else:
            output += "\n - Not yet reserved"
        return output


class MoveHandler:
    def __init__(self,
                reservation_manager: IReservationManager,
                labware_registry: ILabwareRegistry,
                system_map: SystemMap) -> None:
        self._reservation_manager = reservation_manager
        self._labware_registry = labware_registry
        self._system_map = system_map

    async def resolve_move_action(self, labware: Labware, current_location: Location, target_location: Location, assigned_action: LocationAction | None = None) -> MoveAction:
        potential_paths = self._get_potential_paths(current_location, target_location)
        potential_moves = self._get_potential_move_actions(labware, potential_paths)
        if assigned_action is not None:
            self._assign_reservation_to_moves(potential_moves, assigned_action)
        # check for any moves using the assigned_action's reservation
        for action in potential_moves:
            if action.reservation.completed.is_set():
                return action
        route_reservation = MoveActionCollectionReservationRequest(potential_moves)
        reserved_move_action = await route_reservation.reserve_route(self._reservation_manager)
        return reserved_move_action

    async def handle_deadlock(self, move_action: MoveAction) -> MoveAction:
        # NOTE: Although move_action does not have a reservation and does not need to be released, 
        # even though it is set as completed, it is also deadlocked.  This may lead to confusion and may need to be changed
        # due to this, this handling may work better else where

        potential_paths = self._system_map.get_shortest_paths_to_deadlock_resolution(move_action.source.teachpoint_name)
        # if move_action.target is in the potential_paths, remove it
        potential_paths = [path for path in potential_paths if move_action.target.teachpoint_name not in path]
        sorted_paths = sorted(potential_paths, key=lambda path: len(path))
        potential_moves = self._get_potential_move_actions(move_action.labware, sorted_paths)
        route_reservation = MoveActionCollectionReservationRequest(potential_moves)
        reserved_move_action = await route_reservation.reserve_route(self._reservation_manager)

        return reserved_move_action

    def _get_potential_paths(self, current_location: Location, target_location: Location) -> List[List[str]]:
        if current_location == target_location:
            raise ValueError("Source and target locations are the same")
        potential_paths = self._system_map.get_all_shortest_any_paths(current_location.teachpoint_name, target_location.teachpoint_name)
        if potential_paths == []:
            raise ValueError("No routes found between source and target")
        return potential_paths

    def _get_potential_move_actions(self, labware: Labware, potential_paths: List[List[str]]) -> List[MoveAction]:
    
        potential_actions: List[MoveAction] = []
        for path in potential_paths:
            source_location = self._system_map.get_location(path[0])
            target = self._system_map.get_location(path[1])
            transporter = self._system_map.get_transporter_between(source_location.name, target.name)
            action = MoveAction(labware, source_location, target, transporter)
            potential_actions.append(action)
        return potential_actions
    
    def _assign_reservation_to_moves(self, potential_moves: List[MoveAction], assigned_action: LocationAction) -> None:
        # TODO: Fix this later - this is a temporary fix
        # use the reservation already set by the assigned action
        for move in potential_moves:
            if move.target == assigned_action.location:
                move.set_reservation(assigned_action.reservation)
                move.set_release_reservation_on_place(False)
