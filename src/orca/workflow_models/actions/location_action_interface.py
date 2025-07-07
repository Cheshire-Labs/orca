from orca.resource_models.devices import Device
from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.resource_models.location import Location
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.workflow_models.action_template import Device
from orca.workflow_models.actions.util import AssignedLabwareManager


import asyncio
from abc import ABC, abstractmethod
from typing import List


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

    @abstractmethod
    async def execute(self) -> None:
        pass