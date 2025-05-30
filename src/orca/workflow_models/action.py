from abc import ABC, abstractmethod
import asyncio
import logging
from typing import Any, Dict, List, Union, cast
import uuid

from orca.resource_models.base_resource import Device
from orca.resource_models.labware import AnyLabwareTemplate, Labware, LabwareTemplate
from orca.resource_models.location import Location
from orca.resource_models.transporter_resource import TransporterEquipment
from orca.sdk.events.execution_context import ActionExecutionContext, MethodExecutionContext
from orca.sdk.events.event_bus_interface import IEventBus
from orca.system.labware_registry_interfaces import ILabwareRegistry
from orca.system.reservation_manager import IReservationManager, LocationReservation
from orca.system.system_map import SystemMap
from orca.workflow_models.labware_thread import StatusManager
from orca.workflow_models.status_enums import ActionStatus

orca_logger = logging.getLogger("orca")


class AssignedLabwareManager:
    def __init__(self,
                 expected_input_templates: List[Union[LabwareTemplate, AnyLabwareTemplate]],
                 expected_output_templates: List[Union[LabwareTemplate, AnyLabwareTemplate]]) -> None:
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
            missing_inputs = [key.name for key, input in self._expected_inputs.items() if input is None]
            raise ValueError(f"Not all expected inputs have been assigned.  Missing: {missing_inputs}")
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

class BaseAction(ABC):
    def __init__(self) -> None:
        self._id: str = str(uuid.uuid4())

    @property
    def id(self) -> str:
        return self._id


    
    def reset(self) -> None:
        self._status = ActionStatus.CREATED

class BaseActionExecutor(ABC):
    def __init__(self, status_manager: StatusManager):
        self._status_manager = status_manager
        self._lock = asyncio.Lock()
        self.status = ActionStatus.CREATED

    @property
    @abstractmethod
    def status(self) -> ActionStatus:
        raise NotImplementedError

    @status.setter
    @abstractmethod
    def status(self, status: ActionStatus) -> None:
        raise NotImplementedError

    @abstractmethod
    async def _perform_action(self) -> None:
        raise NotImplementedError

    async def execute(self) -> None:
        async with self._lock:
            if self.status == ActionStatus.COMPLETED:
                return
            if self.status == ActionStatus.ERRORED:
                raise ValueError("Action has errored, cannot execute")
            try:
                await self._perform_action()
            except Exception as e:
                self.status = ActionStatus.ERRORED
                raise e        
            self.status = ActionStatus.COMPLETED





class LocationActionData(BaseAction):
    def __init__(self, location: Location, command: str, options: Dict[str, Any] = {}) -> None:
        super().__init__()
        if location.resource is None or not isinstance(location.resource, Device):
            raise ValueError(f"Location {location} does not have an {type(Device)} resource")
        self._location: Location = location
        self._command: str = command
        self._options: Dict[str, Any] = options
        self._reservation: LocationReservation = LocationReservation(location, None)

    @property
    def location(self) -> Location:
        return self._location

    @property
    def command(self) -> str:
        return self._command

    @property
    def options(self) -> Dict[str, Any]:
        return self._options
    
    @property
    def resource(self) -> Device:
        return cast(Device, self._location.resource)
    
    @property
    def reservation(self) -> LocationReservation:
        return self._reservation
    
    def release_reservation(self) -> None:
        self._reservation.release_reservation()

    def __str__(self) -> str:
        return f"Location Action: {self.location} - {self._command}"
    

class LocationActionExecutor(BaseActionExecutor):
    def __init__(self,
                action: LocationActionData,
                 status_manager: StatusManager,
                 context: MethodExecutionContext,
                 assigned_labware_manager: AssignedLabwareManager,
                 ) -> None:
        super().__init__(status_manager)   
        self._context = context
        self._action = action
        self._assigned_labware_manager: AssignedLabwareManager = assigned_labware_manager
        self.is_executing: asyncio.Event = asyncio.Event()
    
    @property
    def expected_inputs(self) -> List[Labware]:
        return self._assigned_labware_manager.expected_inputs
    
    @property
    def expected_outputs(self) -> List[Labware]:
        return self._assigned_labware_manager.expected_outputs
    
    def assign_input(self, template_slot: LabwareTemplate, input: Labware):
        self._assigned_labware_manager.assign_input(template_slot, input)

    @property
    def status(self) -> ActionStatus:
        status = self._status_manager.get_status(self._action.id)
        return ActionStatus[status]

    @status.setter
    def status(self, status: ActionStatus) -> None:
        id = self._action.id
        context = ActionExecutionContext(
                                 self._context.workflow_id,
                                 self._context.workflow_name,
                                 self._context.thread_id,
                                    self._context.thread_name,
                                    self._context.method_id,
                                    self._context.method_name,
                                    id,
                                    status.name.upper(),
                             )
        self._status_manager.set_status("ACTION", id, status.name, context)

    async def _perform_action(self) -> None:
        # TODO: check the correct labware is present
        if len(self.get_missing_input_labware()) > 0:
            raise ValueError("Not all expected labware is present")

        self._status = ActionStatus.PERFORMING_ACTION
        self.is_executing.set()

        # Execute the action
        if self._action.resource is not None:
            await self._action.resource.execute(self._action.command, self._action.options)

    def get_missing_input_labware(self) -> List[Labware]:
        loaded_labwares = self._action.resource.loaded_labware[:]
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
        loaded_labwares = self._action.resource.loaded_labware[:]
        present_labware: List[Labware] = []

        for labware in self._assigned_labware_manager.expected_outputs:
            if labware in loaded_labwares:
                present_labware.append(labware)
                loaded_labwares.remove(labware)

        return present_labware
    
    def all_output_labware_removed(self) -> bool:
        return len(self.get_present_output_labware()) == 0
    






class MoveActionData(BaseAction):
    def __init__(self, 
                 labware: Labware,
                 source: Location,
                 target: Location,
                 transporter: TransporterEquipment):
        super().__init__()
        self._labware = labware
        self._source = source
        self._target = target
        self._transporter = transporter
        self._release_reservation_on_place = True
        self._reservation = LocationReservation(self.target, self.labware)
    
    @property
    def labware(self) -> Labware:
        return self._labware

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
    def  release_reservation_on_place(self) -> bool:
        return self._release_reservation_on_place
    
    @property
    def reservation(self) -> LocationReservation:
        return self._reservation
    
    def set_reservation(self, reservation: LocationReservation) -> None:
        self._reservation = reservation

    def set_release_reservation_on_place(self, release: bool) -> None:
        self._release_reservation_on_place = release
        

class MoveActionExecutor(BaseActionExecutor):
    def __init__(self,
                 status_manager: StatusManager,
                 context: MethodExecutionContext,
                 action: MoveActionData) -> None:
        super().__init__(status_manager)
        self._action = action
        self._context = context
        self.status = ActionStatus.CREATED
        self.status = ActionStatus.AWAITING_MOVE_RESERVATION

    @property
    def status(self) -> ActionStatus:
        status = self._status_manager.get_status(self._action.id)
        return ActionStatus[status]

    @status.setter
    def status(self, status: ActionStatus) -> None:
        id = self._action.id
        context = ActionExecutionContext(
                                 self._context.workflow_id,
                                 self._context.workflow_name,
                                 self._context.thread_id,
                                    self._context.thread_name,
                                    self._context.method_id,
                                    self._context.method_name,
                                    id,
                                    status.name.upper(),
                             )
        self._status_manager.set_status("ACTION", id, status.name, context)
    
    async def perform_action(self) -> None:
        if self._action.reservation is None:
            raise ValueError("Reservation must be set before performing action")
        if self._action.labware is None:
            raise ValueError("Labware must be set before performing action")
        if self._action.target.labware is not None:
            raise ValueError("Target location is occupied")

        self.status = ActionStatus.MOVING
        # move the labware
        await self._action.source.prepare_for_pick(self._action.labware)
        await self._action.target.prepare_for_place(self._action.labware)

        await self._action.transporter.pick(self._action.source)
        await self._action.source.notify_picked(self._action.labware)
        
        await self._action.transporter.place(self._action.target)
        await self._action.target.notify_placed(self._action.labware)
        # await notify_picked
        if self._action.release_reservation_on_place:
            self._action.reservation.release_reservation()


class LocationActionCollectionReservationRequest:
    def __init__(self, location_actions: List[LocationActionData]) -> None:
        self._location_action_requests = location_actions
        self._reserved_action: LocationActionData | None = None

    async def reserve_location(self, reservation_manager: IReservationManager, reference_point: Location, system_map: SystemMap) -> LocationActionData:
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


    def _get_ordered_location_actions(self, reference_point: Location, system_map: SystemMap) -> List[LocationActionData]:
        return sorted(self._location_action_requests, key=lambda x: system_map.get_distance(reference_point.teachpoint_name, x.location.teachpoint_name))


    def __str__(self) -> str:
        output =  f"Location Action Reservation: Resource Pool: {[r.location.teachpoint_name for r in self._location_action_requests]}"
        if self._reserved_action:
            output += f" - Reserved Location: {self._reserved_action.location.teachpoint_name}"
        else:
            output += " - Not yet reserved"
        return output
