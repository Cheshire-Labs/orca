import asyncio
import logging
from orca.resource_models.labware import AnyLabwareTemplate, LabwareInstance, LabwareTemplate
from orca.resource_models.location import Location
from orca.resource_models.resource_pool import EquipmentResourcePool
from orca.system.reservation_manager.interfaces import IReservationCollection, IThreadReservationCoordinator
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.system_map import IResourceLocator, SystemMap


from typing import Dict, List, Set, Union
orca_logger = logging.getLogger("orca")

class LocationCollectionReservationRequest(IReservationCollection):
    def __init__(self, thread_id: str, locations: List[LocationReservation], system_map: SystemMap, reference_point: Location) -> None:
        self._thread_id = thread_id
        self._action_location_requests = locations
        self._reserved_action_location: LocationReservation | None = None
        self._system_map: SystemMap = system_map
        self._reference_point: Location = reference_point
        self._processed = asyncio.Event()
        self._granted = asyncio.Event()
        self._rejected = asyncio.Event()
        self._deadlocked = asyncio.Event()

    @property
    def thread_id(self) -> str:
        return self._thread_id

    @property
    def processed(self) -> asyncio.Event:
        return self._processed

    @property
    def granted(self) -> asyncio.Event:
        return self._granted
    
    @property
    def rejected(self) -> asyncio.Event:
        return self._rejected
    
    @property
    def deadlocked(self) -> asyncio.Event:
        return self._deadlocked
    
    @property
    def reserved_action_location(self) -> LocationReservation:
        if self._reserved_action_location is None:
            raise ValueError("No action location reserved")
        return self._reserved_action_location

    # async def reserve_location(self,
    #                            thread_reservation_manager: IThreadReservationCoordinator,
    #                            reference_point: Location,
    #                            system_map: SystemMap) -> LocationReservation:
    #     self._system_map = system_map
    #     self._reference_point = reference_point
    #     thread_reservation_manager.add_reservation_collection(self)
    #     for request in self._action_location_requests:
    #         request.submit_reservation_request(thread_reservation_manager)

    #     # await first location reservation completed
    #     done, pending = await asyncio.wait([asyncio.create_task(r.granted.wait()) for r in requests], return_when=asyncio.FIRST_COMPLETED)

    #     # cancel all pending tasks
    #     for task in pending:
    #         task.cancel()

    #     # get first completed route, release all completed, and cancel the rest
    #     for request in requests:
    #         if request.granted.is_set():
    #             if self._reserved_action_location is None:
    #                 self._reserved_action_location = request
    #             else:
    #                 thread_reservation_manager.release_reservation(request.reserved_location.teachpoint_name)
    #         else:
    #             request.rejected.set()

    #     if self._reserved_action_location is None:
    #         raise ValueError("No location reserved")
    #     return self._reserved_action_location
    

    def resolve_final_reservation(self) -> None:
        sorted_requests = self._sort_requests(self._reference_point, self._system_map)
        granted_reservations = [r for r in sorted_requests if r.granted.is_set()]
        if len(granted_reservations) == 0:
            self._rejected.set()
            self._processed.set()
            return

        # choose the first granted reservation as the final reservation
        self._reserved_action_location = granted_reservations[0] if granted_reservations else None

        # release all other reservations
        for reservation in granted_reservations[1:]:
            reservation.release_reservation()

        # set the granted event
        self._granted.set()
        self._processed.set()


    def _sort_requests(self, reference_point: Location, system_map: SystemMap) -> List[LocationReservation]:
        return sorted(self._action_location_requests,
                      key=lambda x: system_map.get_distance(reference_point.teachpoint_name, x.requested_location.teachpoint_name))
    
    def get_reservations(self) -> List:
        return self._action_location_requests
    
    def clear(self) -> None:
        """Clears the reservation collection, resetting all events and states."""
        if self.granted.is_set():
            # May re-examine if this should be allowed - I haven't looked into the implications yet
            raise ValueError("Cannot clear a reservation that has been granted")
        for action in self._action_location_requests:
            action.clear()
        self._processed.clear()
        self._rejected.clear()
        self._deadlocked.clear()

    def __str__(self) -> str:
        output =  f"Location Action Reservation: Resource Pool: {[r.requested_location.teachpoint_name for r in self._action_location_requests]}"
        if self._reserved_action_location:
            output += f" - Reserved Location: {self._reserved_action_location.reserved_location.teachpoint_name}"
        else:
            output += " - Not yet reserved"
        return output


class ResourcePoolResolver:
    def __init__(self,
                resource_pool: EquipmentResourcePool) -> None:
        self._resource_pool = resource_pool
        self._resolved_location: LocationReservation | None = None

    async def resolve_action_location(self,
                            thread_id: str,
                             reference_point: Location,
                             thread_reservation_manager: IThreadReservationCoordinator,
                             system_map: SystemMap) -> LocationReservation:
        if self._resolved_location is not None:
            return self._resolved_location
        reservation_request_collection = LocationCollectionReservationRequest(thread_id,
                                                                               self._get_potential_action_locations(system_map),
                                                                               system_map, 
                                                                               reference_point)
        await thread_reservation_manager.submit_reservation_request(thread_id, reservation_request_collection)
        await reservation_request_collection.processed.wait()
        if reservation_request_collection.deadlocked.is_set():
            await asyncio.sleep(0.2)
            orca_logger.info("Reservation request collection is deadlocked, retrying")
            reservation_request_collection.clear()
            return await self.resolve_action_location(thread_id, reference_point, thread_reservation_manager, system_map)
        if reservation_request_collection.rejected.is_set():
            await asyncio.sleep(0.2)
            orca_logger.info("Reservation request collection was rejected, retrying")
            reservation_request_collection.clear()
            return await self.resolve_action_location(thread_id, reference_point, thread_reservation_manager, system_map)
        if reservation_request_collection.granted.is_set():
            return reservation_request_collection.reserved_action_location
        raise ValueError("Reservation request collection was not granted")

    def _get_potential_action_locations(self, resource_locator: IResourceLocator) -> List[LocationReservation]:
        potential_locations: Set[Location] = set()
        for resource in self._resource_pool.resources:
            potential_location = resource_locator.get_resource_location(resource.name)
            potential_locations.add(potential_location)

        location_requests: List[LocationReservation] = []
        for location in potential_locations:
            location_request = LocationReservation(location, None)
            location_requests.append(location_request)
            # location_action = LocationActionData(location,
            #                                      self._action.command,
            #                                  self._action.options)
            # location_actions.append(location_action)
        return location_requests


class AssignedLabwareManager:
    def __init__(self,
                 expected_input_templates: List[Union[LabwareTemplate, AnyLabwareTemplate]],
                 expected_output_templates: List[Union[LabwareTemplate, AnyLabwareTemplate]]) -> None:
        self._expected_input_templates = expected_input_templates
        self._expected_inputs: Dict[LabwareTemplate | AnyLabwareTemplate, LabwareInstance | None] = {template: None for template in expected_input_templates}
        self._expected_output_templates = expected_output_templates
        self._expected_outputs: Dict[LabwareTemplate | AnyLabwareTemplate, LabwareInstance | None] = {template: None for template in expected_output_templates}

    @property
    def expected_input_templates(self) -> List[Union[LabwareTemplate, AnyLabwareTemplate]]:
        return self._expected_input_templates

    @property
    def expected_output_templates(self) -> List[Union[LabwareTemplate, AnyLabwareTemplate]]:
        return self._expected_output_templates

    @property
    def expected_inputs(self) -> List[LabwareInstance]:
        if any(input is None for input in self._expected_inputs.values()):
            missing_inputs = [key.name for key, input in self._expected_inputs.items() if input is None]
            raise ValueError(f"Not all expected inputs have been assigned.  Missing: {missing_inputs}")
        return [labware for labware in self._expected_inputs.values() if labware is not None]

    @property
    def expected_outputs(self) -> List[LabwareInstance]:
        if any(output is None for output in self._expected_outputs.values()):
            raise ValueError("Not all expected outputs have been assigned")
        return [labware for labware in self._expected_outputs.values() if labware is not None]

    def assign_input(self, template_slot: LabwareTemplate, input: LabwareInstance):
        if template_slot in self._expected_inputs.keys():
            self._expected_inputs[template_slot] = input
        elif any(input is None and isinstance(key, AnyLabwareTemplate) for key, input in self._expected_inputs.items()):
            for key in self._expected_inputs.keys():
                if isinstance(key, AnyLabwareTemplate):
                    self._expected_inputs[key] = input
                    break

        else:
            raise ValueError(f"No available slot for input {input}")
        # TODO: keeping this assignment simple for now
        self.assign_output(template_slot, input)

    def assign_output(self, template_slot: LabwareTemplate, output: LabwareInstance):
        if template_slot in self._expected_outputs.keys():
            self._expected_outputs[template_slot] = output
        elif any(output is None and isinstance(key, AnyLabwareTemplate) for key, output in self._expected_outputs.items()):
            for key in self._expected_outputs.keys():
                if isinstance(key, AnyLabwareTemplate):
                    self._expected_outputs[key] = output
                    break
        else:
            raise ValueError(f"No available slot for output {output}")

    def __str__(self) -> str:
        return f"Input Manager: {self._expected_inputs}"