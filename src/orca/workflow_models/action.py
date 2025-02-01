from abc import ABC, abstractmethod
import asyncio
import logging
from typing import Any, Callable, Dict, List, Union, cast
import uuid

from orca.resource_models.base_resource import LabwareLoadableEquipment
from orca.resource_models.labware import AnyLabwareTemplate, Labware, LabwareTemplate
from orca.resource_models.location import Location
from orca.resource_models.transporter_resource import TransporterEquipment
from orca.system.labware_registry_interfaces import ILabwareRegistry
from orca.system.reservation_manager import IReservationManager, LocationReservation
from orca.system.system_map import SystemMap
from orca.workflow_models.status_enums import ActionStatus

orca_logger = logging.getLogger("orca")
class IActionObserver:
    def action_notify(self, event: str, action: 'BaseAction') -> None:
        pass

class ActionObserver(IActionObserver):
    def __init__(self, callback: Callable[[str, 'BaseAction'], None]) -> None:
        self._callback = callback
    
    def action_notify(self, event: str, action: 'BaseAction') -> None:
        self._callback(event, action)

class BaseAction(ABC):
    def __init__(self) -> None:
        self._id: str = str(uuid.uuid4())
        self.__status: ActionStatus = ActionStatus.CREATED
        self._observers: List[IActionObserver] = []
        self._lock = asyncio.Lock()

    @property
    def id(self) -> str:
        return self._id

    @property
    def status(self) -> ActionStatus:
        return self._status
    
    @property
    def _status(self) -> ActionStatus:
        return self.__status

    @_status.setter
    def _status(self, status: ActionStatus) -> None:
        self.__status = status
        for observer in self._observers:
            observer.action_notify(self.__status.name.upper(), self)

    async def execute(self) -> None:
        async with self._lock:
            if self._status == ActionStatus.COMPLETED:
                return
            if self._status == ActionStatus.ERRORED:
                raise ValueError("Action has errored, cannot execute")
            try:
                await self._perform_action()
            except Exception as e:
                self._status = ActionStatus.ERRORED
                raise e        
            self._status = ActionStatus.COMPLETED


    @abstractmethod
    async def _perform_action(self) -> None:
        raise NotImplementedError
    
    def reset(self) -> None:
        self._status = ActionStatus.CREATED

    def add_observer(self, observer: IActionObserver) -> None:
        if observer not in self._observers:
            self._observers.append(observer)


class AssignedLabwareManager:
    def __init__(self,
                 labware_registry: ILabwareRegistry,
                 expected_input_templates: List[Union[LabwareTemplate, AnyLabwareTemplate]],
                 expected_output_templates: List[Union[LabwareTemplate, AnyLabwareTemplate]]) -> None:
        self._labware_reg = labware_registry
        self._expected_input_templates = expected_input_templates
        self._expected_inputs: Dict[LabwareTemplate | AnyLabwareTemplate, Labware | None] = {template: None for template in expected_input_templates}
        self._expected_output_templates = expected_output_templates
        self._expected_outputs: Dict[LabwareTemplate | AnyLabwareTemplate, Labware | None] = {template: None for template in expected_output_templates}

    @property
    def expected_input_templates(self) -> List[Union[LabwareTemplate, AnyLabwareTemplate]]:
        return self._expected_input_templates

    @property
    def expected_output_templates(self) -> List[Union[LabwareTemplate, AnyLabwareTemplate]]:
        return self._expected_output_templates

    @property
    def expected_inputs(self) -> List[Labware]:
        if any(input is None for input in self._expected_inputs.values()):
            raise ValueError("Not all expected inputs have been assigned")
        return [labware for labware in self._expected_inputs.values() if labware is not None]

    @property
    def expected_outputs(self) -> List[Labware]:
        if any(output is None for output in self._expected_outputs.values()):
            raise ValueError("Not all expected outputs have been assigned")
        return [labware for labware in self._expected_outputs.values() if labware is not None]
        
    def assign_input(self, template_slot: LabwareTemplate, input: Labware):
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
        
    def assign_output(self, template_slot: LabwareTemplate, output: Labware):
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


class LocationAction(BaseAction):
    def __init__(self,
                 location: Location,
                 command: str,
                 assigned_labware_manager: AssignedLabwareManager,
                 options: Dict[str, Any] = {}) -> None:
        super().__init__()   
        if location.resource is None or not isinstance(location.resource, LabwareLoadableEquipment):
            raise ValueError(f"Location {location} does not have an {type(LabwareLoadableEquipment)} resource")
        self._location = location
        self._command: str = command
        self._options: Dict[str, Any] = options
        self._assigned_labware_manager: AssignedLabwareManager = assigned_labware_manager
        self.is_executing: asyncio.Event = asyncio.Event()
        self._reservation: LocationReservation = LocationReservation(location, None)

    @property
    def resource(self) -> LabwareLoadableEquipment:
        return cast(LabwareLoadableEquipment, self._location.resource)

    @property
    def location(self) -> Location:
        return self._location
    
    @property
    def expected_inputs(self) -> List[Labware]:
        return self._assigned_labware_manager.expected_inputs
    
    @property
    def expected_outputs(self) -> List[Labware]:
        return self._assigned_labware_manager.expected_outputs
    
    @property
    def reservation(self) -> LocationReservation:
        return self._reservation
    
    def assign_input(self, template_slot: LabwareTemplate, input: Labware):
        self._assigned_labware_manager.assign_input(template_slot, input)

    async def _perform_action(self) -> None:
        # TODO: check the correct labware is present
        if len(self.get_missing_input_labware()) > 0:
            raise ValueError("Not all expected labware is present")

        self._status = ActionStatus.PERFORMING_ACTION
        self.is_executing.set()
        # TODO: DELETE DELETE DELETE
        # for Labware in self.resource.loaded_labware:
        #    orca_logger.debug(f"LOADED LABWARE: {Labware.name}")

        # Execute the action
        if self.resource is not None:
            await self.resource.execute(self._command, self._options)

    def get_missing_input_labware(self) -> List[Labware]:
        loaded_labwares = self.resource.loaded_labware[:]
        missing_labware: List[Labware] = []

        for labware in self._assigned_labware_manager.expected_inputs:
            if labware not in loaded_labwares:
                missing_labware.append(labware)
            else:
                loaded_labwares.remove(labware)

        if len(missing_labware) > 0:
            self._status = ActionStatus.AWAITING_CO_THREADS
        return missing_labware
    
    def all_input_labware_present(self) -> bool:
        return len(self.get_missing_input_labware()) == 0
    
    def get_present_output_labware(self) -> List[Labware]:
        loaded_labwares = self.resource.loaded_labware[:]
        present_labware: List[Labware] = []

        for labware in self._assigned_labware_manager.expected_outputs:
            if labware in loaded_labwares:
                present_labware.append(labware)
                loaded_labwares.remove(labware)

        return present_labware
    
    def all_output_labware_removed(self) -> bool:
        return len(self.get_present_output_labware()) == 0
    
    def release_reservation(self) -> None:
        self._reservation.release_reservation()

    def __str__(self) -> str:
        return f"Location Action: {self.location} - {self._command}"

class MoveAction(BaseAction):
    def __init__(self,
                 labware: Labware,
                 source: Location,
                 target: Location,
                 transporter: TransporterEquipment) -> None:
        super().__init__()
        self._labware = labware
        self._source = source
        self._target = target
        self._transporter: TransporterEquipment = transporter
        self._reservation = LocationReservation(self._target, labware)
        self._release_reservation_on_place = True
        self._status = ActionStatus.AWAITING_MOVE_RESERVATION

    @property
    def source(self) -> Location:
        return self._source

    @property
    def target(self) -> Location:
        return self._target
    
    @property
    def transporter(self) -> TransporterEquipment:
        return self._transporter
    
    @property
    def labware(self) -> Labware:
        return self._labware
    
    @property
    def reservation(self) -> LocationReservation:
        return self._reservation
    
    def set_reservation(self, reservation: LocationReservation) -> None:
        self._reservation = reservation

    def set_release_reservation_on_place(self, release: bool) -> None:
        self._release_reservation_on_place = release
    
    async def _perform_action(self) -> None:
        if self._reservation is None:
            raise ValueError("Reservation must be set before performing action")
        if self._labware is None:
            raise ValueError("Labware must be set before performing action")

        if self._target.labware is not None:
            raise ValueError("Target location is occupied")

        self._status = ActionStatus.MOVING
        # move the labware
        await self._source.prepare_for_pick(self._labware)
        await self._target.prepare_for_place(self._labware)

        await self._transporter.pick(self._source)
        await self._source.notify_picked(self._labware)
        
        await self._transporter.place(self._target)
        await self._target.notify_placed(self._labware)
        # await notify_picked
        if self._release_reservation_on_place:
            self._reservation.release_reservation()


class LocationActionCollectionReservationRequest:
    def __init__(self, location_actions: List[LocationAction]) -> None:
        self._location_action_requests = location_actions
        self._reserved_action: LocationAction | None = None

    async def reserve_location(self, reservation_manager: IReservationManager, reference_point: Location, system_map: SystemMap) -> LocationAction:
        location_actions = self._get_ordered_location_actions(reference_point, system_map)
        for request in location_actions:
            request.reservation.request_reservation(reservation_manager)
            
        # await first location reservation completed
        done, pending = await asyncio.wait([asyncio.create_task(action.reservation.completed.wait()) for action in location_actions], return_when=asyncio.FIRST_COMPLETED)
        
        # cancel all pending tasks
        for task in pending:
            task.cancel()

        # get first completed route, release all completed, and cancel the rest
        for request in location_actions:
            if request.reservation.completed.is_set():
                if self._reserved_action is None:
                    self._reserved_action = request
                else:
                    reservation_manager.release_reservation(request.reservation.reserved_location.teachpoint_name)
            else:
                request.reservation.cancelled.set()

        if self._reserved_action is None:
            raise ValueError("No location reserved")
        return self._reserved_action

    def release_reservation(self) -> None:
        if self._reserved_action:
            self._reserved_action.reservation.release_reservation()
            self._reserved_action = None
        else:
            raise ValueError("No reservation to release")


    def _get_ordered_location_actions(self, reference_point: Location, system_map: SystemMap) -> List[LocationAction]:
        return sorted(self._location_action_requests, key=lambda x: system_map.get_distance(reference_point.teachpoint_name, x.location.teachpoint_name))


    def __str__(self) -> str:
        output =  f"Location Action Reservation: Resource Pool: {[r.location.teachpoint_name for r in self._location_action_requests]}"
        if self._reserved_action:
            output += f" - Reserved Location: {self._reserved_action.location.teachpoint_name}"
        else:
            output += " - Not yet reserved"
        return output
