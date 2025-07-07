from abc import ABC, abstractmethod
import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, cast
import uuid

from orca.driver_management.driver_interfaces import ICentrifuge, IDelidder, IGenericExecutable, IProtocolRunner, IReader, ISealer, IShaker
from orca.resource_models.devices import Device
from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.resource_models.location import Location
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.workflow_models.action_template import Any, Device
from orca.workflow_models.actions.location_action_interface import ILocationAction
from orca.workflow_models.actions.util import AssignedLabwareManager
from orca.workflow_models.status_enums import ActionStatus

orca_logger = logging.getLogger("orca")
class LocationAction(ILocationAction, ABC):
    def __init__(self, 
                 command: str, 
                 ) -> None:
        self._id: str = str(uuid.uuid4())
        self._command = command
        self._reservation: LocationReservation | None = None
        self._assigned_labware_manager: AssignedLabwareManager | None = None
        self._all_labware_is_present = asyncio.Event()

    @property
    def assigned_labware_manager(self) -> AssignedLabwareManager:
        if self._assigned_labware_manager is None:
            raise ValueError("AssignedLabwareManager is not set. Please set it before accessing.")
        return self._assigned_labware_manager

    def set_assigned_labware_manager(self, assigned_labware_manager: AssignedLabwareManager) -> None:
        self._assigned_labware_manager = assigned_labware_manager


    def set_location_reservation(self, reservation: LocationReservation) -> None:
        self._reservation = reservation


    @property
    def id(self) -> str:
        return self._id

    @property
    def location(self) -> Location:
        return self.reservation.reserved_location

    @property
    def command(self) -> str:
        return self._command

    # @property
    # def options(self) -> Dict[str, Any]:
    #     return self._options
    
    @property
    def resource(self) -> Device:
        return cast(Device, self.location.resource)
    
    @property
    def expected_inputs(self) -> List[LabwareInstance]:
        return self.assigned_labware_manager.expected_inputs
    
    @property
    def expected_outputs(self) -> List[LabwareInstance]:
        return self.assigned_labware_manager.expected_outputs
    
    def assign_input(self, template_slot: LabwareTemplate, input: LabwareInstance):
        self.assigned_labware_manager.assign_input(template_slot, input)
    
    @property
    def reservation(self) -> LocationReservation:
        if self._reservation is None:
            raise ValueError("Location reservation is not set. Please set it before accessing.")
        return self._reservation
    
    def release_reservation(self) -> None:
        self.reservation.release_reservation()

    def get_missing_input_labware(self) -> List[LabwareInstance]:
        loaded_labwares = self.resource.loaded_labware[:]
        missing_labware: List[LabwareInstance] = []

        for labware in self.assigned_labware_manager.expected_inputs:
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

        for labware in self.assigned_labware_manager.expected_outputs:
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
    
    def __str__(self) -> str:
        return f"Location Action: {self.location} - {self._command}"
    
    @abstractmethod
    async def execute(self) -> None:
        raise NotImplementedError("Subclasses must implement the execute method.")
    

class ExecuteCommandAction(LocationAction):
    def __init__(self, 
                 command: str, 
                 options: Dict[str, Any],
                 ) -> None:
        super().__init__(command)
        self._options = options

    async def execute(self) -> None:
        if not isinstance(self.resource, IGenericExecutable):
            raise TypeError("Resource must implement IGenericExecutable.")
        await self.resource.execute(self._command, self._options)

type ExecuteMethodType = Callable[[Device, List[LabwareInstance], List[LabwareInstance], Dict[str, Any]], Awaitable[None]]

class ExecuteMethodAction(LocationAction):
    def __init__(self,
                 command: str,
                 method: ExecuteMethodType,
                 options: Dict[str, Any] | None = None):
        self._method = method
        self._options = options or {}
        super().__init__(command)

    async def execute(self) -> None:
        assigned_inputs = self.assigned_labware_manager.expected_inputs
        assigned_outputs = self.assigned_labware_manager.expected_outputs
        async with self.resource.lock:
            await self._method(
                self.resource,
                assigned_inputs,
                assigned_outputs,
                self._options
            )

class RunProtocolAction(LocationAction):
    def __init__(self, 
                 command: str,
                protocol_filepath: str,
                 parameters: Dict[str, Any],
                 options: Dict[str, Any],
                 ) -> None:
        super().__init__(command)
        self._protocol_filepath = protocol_filepath
        self._parameters = parameters
        self._options = options

    async def execute(self) -> None:
        if not isinstance(self.resource, IProtocolRunner):
            raise TypeError("Resource must implement IProtocolRunner.")
        async with self.resource.lock:
            await self.resource.run_protocol(self._protocol_filepath, self._parameters)

class SealLocationAction(LocationAction):
    def __init__(self,
                 command: str,
                temperature: int,
                 duration: float,
                 ):
        super().__init__(command)
        self._temperature = temperature
        self._duration = duration

    async def execute(self) -> None:
        if not isinstance(self.resource, ISealer):
            raise TypeError("Resource must implement ISealer.")
        async with self.resource.lock:
            await self.resource.seal(self._temperature, self._duration)

class ShakeLocationAction(LocationAction):
    def __init__(self,
                 command: str,
                 speed: int,
                 duration: int,
                 ):
        super().__init__(command)
        self._speed = speed
        self._duration = duration

    async def execute(self) -> None:
        if not isinstance(self.resource, IShaker):
            raise TypeError("Resource must implement IShaker.")
        async with self.resource.lock:
            await self.resource.shake(self._duration, self._speed)


class CentrifugeLocationAction(LocationAction):
    def __init__(self,
                 command: str,
                 speed: int,
                 duration: int,
                 ):
        super().__init__(command)
        self._speed = speed
        self._duration = duration

    async def execute(self) -> None:
        if not isinstance(self.resource, ICentrifuge):
            raise TypeError("Resource must implement ICentrifuge.")
        async with self.resource.lock:
            await self.resource.centrifuge(self._duration, self._speed)

class ReadLocationAction(LocationAction):
    def __init__(self,
                 command: str,
                protocol_filepath: str,
                output_filepath: str,
                 ):
        super().__init__(command)
        self.protocol_filepath = protocol_filepath
        self.output_filepath = output_filepath

    async def execute(self) -> None:
        if not isinstance(self.resource, IReader):
            raise TypeError("Resource must implement IReader.")
        async with self.resource.lock:
            await self.resource.read(self.protocol_filepath, self.output_filepath)


class DelidLocationAction(LocationAction):
    def __init__(self, command: str) -> None:
        super().__init__(command)

    async def execute(self) -> None:
        if not isinstance(self.resource, IDelidder):
            raise TypeError("Resource must implement IDelidder.")
        async with self.resource.lock:
            await self.resource.delid()