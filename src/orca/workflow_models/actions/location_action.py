from abc import ABC
import asyncio
import logging
from typing import Any, Dict, List, Optional, cast
import uuid

from orca.events.execution_context import LocationActionExecutionContext, MethodExecutionContext
from orca.resource_models.base_resource import Device
from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.resource_models.location import Location
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.workflow_models.actions.util import AssignedLabwareManager
from orca.workflow_models.status_enums import ActionStatus
from orca.workflow_models.status_manager import StatusManager
from abc import ABC, abstractmethod

orca_logger = logging.getLogger("orca")
class ILocationAction(ABC):

    @property
    @abstractmethod
    def id(self) -> str:
        pass
    
    @property
    @abstractmethod
    def location(self) -> Location:
        pass

    @property
    @abstractmethod
    def command(self) -> str:
        pass

    @property
    @abstractmethod
    def options(self) -> Dict[str, Any]:
        pass

    @property
    @abstractmethod
    def resource(self) -> Device:
        pass

    @property
    @abstractmethod
    def expected_inputs(self) -> List[LabwareInstance]:
        pass

    @property
    @abstractmethod
    def expected_outputs(self) -> List[LabwareInstance]:
        pass

    @abstractmethod
    def assign_input(self, template_slot: LabwareTemplate, input: LabwareInstance):
        pass

    @property
    @abstractmethod
    def reservation(self) -> LocationReservation:
        pass

    @abstractmethod
    def release_reservation(self) -> None:
        pass

    @abstractmethod
    def get_missing_input_labware(self) -> List[LabwareInstance]:
        pass

    @abstractmethod
    def get_present_output_labware(self) -> List[LabwareInstance]:
        pass

    @abstractmethod
    def all_output_labware_removed(self) -> bool:
        pass

    @property
    @abstractmethod
    def all_labware_is_present(self) -> asyncio.Event:
        pass
        
class LocationAction(ILocationAction):
    def __init__(self, 
                 location_reservation: LocationReservation, 
                 assigned_labware_manager: AssignedLabwareManager,
                 command: str, 
                 options: Optional[Dict[str, Any]] = None) -> None:
        self._id: str = str(uuid.uuid4())
        self._command: str = command
        self._options: Dict[str, Any] = options if options is not None else {}
        self._reservation = location_reservation
        self._assigned_labware_manager = assigned_labware_manager
        self._all_labware_is_present = asyncio.Event()

    @property
    def id(self) -> str:
        return self._id

    @property
    def location(self) -> Location:
        return self._reservation.reserved_location

    @property
    def command(self) -> str:
        return self._command

    @property
    def options(self) -> Dict[str, Any]:
        return self._options
    
    @property
    def resource(self) -> Device:
        return cast(Device, self.location.resource)
    
    @property
    def expected_inputs(self) -> List[LabwareInstance]:
        return self._assigned_labware_manager.expected_inputs
    
    @property
    def expected_outputs(self) -> List[LabwareInstance]:
        return self._assigned_labware_manager.expected_outputs
    
    def assign_input(self, template_slot: LabwareTemplate, input: LabwareInstance):
        self._assigned_labware_manager.assign_input(template_slot, input)
    
    @property
    def reservation(self) -> LocationReservation:
        return self._reservation
    
    def release_reservation(self) -> None:
        self._reservation.release_reservation()

    def __str__(self) -> str:
        return f"Location Action: {self.location} - {self._command}"
    

    def get_missing_input_labware(self) -> List[LabwareInstance]:
        loaded_labwares = self.resource.loaded_labware[:]
        missing_labware: List[LabwareInstance] = []

        for labware in self._assigned_labware_manager.expected_inputs:
            if labware not in loaded_labwares:
                missing_labware.append(labware)
            else:
                loaded_labwares.remove(labware)

        if len(missing_labware) > 0:
            self._status = ActionStatus.AWAITING_CO_THREADS
        else:
            self._all_labware_is_present.set()
        return missing_labware      
    
    def get_present_output_labware(self) -> List[LabwareInstance]:
        loaded_labwares = self.resource.loaded_labware[:]
        present_labware: List[LabwareInstance] = []

        for labware in self._assigned_labware_manager.expected_outputs:
            if labware in loaded_labwares:
                present_labware.append(labware)
                loaded_labwares.remove(labware)

        return present_labware
    
    def all_output_labware_removed(self) -> bool:
        return len(self.get_present_output_labware()) == 0
    
    @property
    def all_labware_is_present(self) -> asyncio.Event:
        if not self._all_labware_is_present.is_set():
            self.get_missing_input_labware()
        return self._all_labware_is_present


class ExecutingLocationAction(ILocationAction):
    def __init__(self,
                 status_manager: StatusManager,
                 action: ILocationAction,
                 context: MethodExecutionContext,
                 ) -> None:
        super().__init__()
        self._status_manager = status_manager
        self._context = context
        self._action = action
        self.status = ActionStatus.CREATED
        self._is_executing = asyncio.Lock()

    @property
    def status(self) -> ActionStatus:
        status = self._status_manager.get_status(self._action.id)
        return ActionStatus[status]

    @status.setter
    def status(self, status: ActionStatus) -> None:
        id = self._action.id
        context = LocationActionExecutionContext(
            self._context.workflow_id,
            self._context.workflow_name,
            self._context.method_id,
            self._context.method_name,
            id,
            status.name.upper(),
        )
        self._status_manager.set_status("ACTION", id, status.name, context)

    async def _execute_action(self) -> None:
        self._status = ActionStatus.AWAITING_CO_THREADS
        await self.all_labware_is_present.wait()
        self._status = ActionStatus.EXECUTING_ACTION
        self._ensure_all_labware_present()

        await self._action.resource.execute(self._action.command, self._action.options)
    

    async def execute(self) -> None:

        async with self._is_executing:
            if self.status == ActionStatus.COMPLETED:
                return
            if self.status == ActionStatus.ERRORED:
                raise ValueError("Action has errored, cannot execute")
            try:
                await self._execute_action()
            except Exception as e:
                self.status = ActionStatus.ERRORED
                raise e
            self.status = ActionStatus.COMPLETED

    def _ensure_all_labware_present(self) -> None:
        missing_labware = self._action.get_missing_input_labware()
        if len(missing_labware) > 0:
            raise ValueError(
                f"Missing labware for action '{self._action.command}' (ID: {self._action.id}) at location '{self._action.location}': "
                f"{', '.join([labware.name for labware in missing_labware])}"
            )
    @property
    def id(self) -> str:
        return self._action.id

    @property
    def location(self) -> Location:
        return self._action.location

    @property
    def command(self) -> str:
        return self._action.command

    @property
    def options(self) -> Dict[str, Any]:
        return self._action.options

    @property
    def resource(self) -> Device:
        return self._action.resource

    @property
    def expected_inputs(self) -> List[LabwareInstance]:
        return self._action.expected_inputs

    @property
    def expected_outputs(self) -> List[LabwareInstance]:
        return self._action.expected_outputs

    def assign_input(self, template_slot: LabwareTemplate, input: LabwareInstance):
        return self._action.assign_input(template_slot, input)

    @property
    def reservation(self) -> LocationReservation:
        return self._action.reservation

    def release_reservation(self) -> None:
        return self._action.release_reservation()

    def get_missing_input_labware(self) -> List[LabwareInstance]:
        return self._action.get_missing_input_labware()

    def get_present_output_labware(self) -> List[LabwareInstance]:
        return self._action.get_present_output_labware()

    def all_output_labware_removed(self) -> bool:
        return self._action.all_output_labware_removed()
    
    @property
    def all_labware_is_present(self) -> asyncio.Event:
        return self._action.all_labware_is_present