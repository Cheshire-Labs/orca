import uuid
from resource_models.base_resource import Equipment
from resource_models.labware import AnyLabware, AnyLabwareTemplate, Labware, LabwareTemplate
from resource_models.location import Location
from resource_models.resource_pool import EquipmentResourcePool
from system.labware_registry_interfaces import ILabwareRegistry
from system.system_map import SystemMap
from workflow_models.action import BaseAction

from typing import Any, Dict, List, Union, cast

from workflow_models.status_enums import ActionStatus


class LocationAction(BaseAction):
    def __init__(self,
                 location: Location,
                 command: str,
                 expected_inputs: List[Union[Labware, AnyLabware]],
                 expected_outputs: List[Labware],
                 options: Dict[str, Any] = {}) -> None:
        super().__init__()
        if location.resource is None or not isinstance(location.resource, Equipment):
            raise ValueError(f"Location {location} does not have an Equipment resource")
        self._location = location
        self._command: str = command
        self._options: Dict[str, Any] = options
        self._expected_inputs = expected_inputs
        self._expected_outputs = expected_outputs

    @property
    def resource(self) -> Equipment:
        return cast(Equipment, self._location.resource)
    
    @property
    def location(self) -> Location:
        return self._location

    def _perform_action(self) -> None:
        # TODO: check the correct labware is present
        if len(self.get_missing_labware()) > 0:
            raise ValueError(f"Missing labware: {self.get_missing_labware()}")


        # TODO: DELETE DELETE DELETE
        for Labware in self.resource.loaded_labware:
            print("LOADED LABWARE: ", Labware.name)

        # Execute the action
        if self.resource is not None:
            self.resource.set_command(self._command)
            self.resource.set_command_options(self._options)
            self.resource.execute()

    def get_missing_labware(self) -> List[Union[Labware, AnyLabware]]:
        loaded_labwares = self.resource.loaded_labware[:]
        any_labware_count = 0
        missing_labware: List[Union[Labware, AnyLabware]] = []

        for labware in self._expected_inputs:
            if isinstance(labware, AnyLabware):
                any_labware_count += 1
                continue
            if labware not in loaded_labwares:
                missing_labware.append(labware)
            else:
                loaded_labwares.remove(labware)
                    
        if len(loaded_labwares) != any_labware_count:
            for _ in range(any_labware_count - len(loaded_labwares)):
                missing_labware.append(AnyLabware())
        if len(missing_labware) > 0:
            self._status = ActionStatus.AWAITING_LABWARES
        return missing_labware

    def __str__(self) -> str:
        return f"Location Action: {self.location} - {self._command}"
    
class DynamicResourceAction:
    def __init__(self,
                 labware_registry: ILabwareRegistry,
                 resource: EquipmentResourcePool | List[Equipment] | Equipment,
                 command: str,
                 expected_inputs: List[Union[LabwareTemplate, AnyLabwareTemplate]],
                 expected_outputs: List[LabwareTemplate],
                 options: Dict[str, Any] = {}) -> None:
        if isinstance(resource, EquipmentResourcePool):
            self._resource_pool: EquipmentResourcePool = resource
        elif isinstance(resource, list):
            self._resource_pool = EquipmentResourcePool(f"Generated Resource Pool - {uuid.uuid4()}", resource)
        elif isinstance(resource, Equipment):
            self._resource_pool = EquipmentResourcePool(f"Generated Resource Pool - {uuid.uuid4()}", [resource])
        self._command: str = command
        self._options: Dict[str, Any] = options
        self._expected_inputs = expected_inputs
        self._expected_outputs = expected_outputs
        self._labware_reg = labware_registry

    @property
    def resource_pool(self) -> EquipmentResourcePool:
        return self._resource_pool
    
    @property
    def expected_inputs(self) -> List[Union[LabwareTemplate, AnyLabwareTemplate]]:
        return self._expected_inputs
    
    @property
    def expected_outputs(self) -> List[LabwareTemplate]:
        return self._expected_outputs


    def resolve_resource_action(self, reference_point: Location, system_map: SystemMap) -> LocationAction:
        resource = self._get_closest_available_resource(self._resource_pool, reference_point, system_map)
        location = system_map.get_resource_location(resource.name)
        return LocationAction(location,
                              self._command,
                              self._resolve_expected_inputs(),
                              self._resolve_expected_outputs(),
                              self._options)
    
    def _resolve_expected_inputs(self) -> List[Union[Labware, AnyLabware]]:
        inputs: List[Union[Labware, AnyLabware]] = []
        for input_template in self._expected_inputs:
            if isinstance(input_template, AnyLabwareTemplate):
                inputs.append(AnyLabware())
            else:
                inputs.append(self._labware_reg.get_labware(input_template.name))
        return inputs

    def _resolve_expected_outputs(self) -> List[Labware]:
        outputs: List[Labware] = [self._labware_reg.get_labware(output.name) for output in self._expected_outputs]
        return outputs

    def _get_closest_available_resource(self, resource_pool: EquipmentResourcePool, reference_point: Location, system_map: SystemMap) -> Equipment:
        available_resources = [resource for resource in resource_pool.resources if resource.is_available]
        ordered_resources = sorted(available_resources, key=lambda x: 
                                   system_map.get_distance(reference_point.teachpoint_name, 
                                                       system_map.get_resource_location(x.name).teachpoint_name))
        if not ordered_resources:
            raise ValueError(f"{self}: No available resources")
        return ordered_resources[0]
    
    def __str__(self) -> str:
        return f"Resource Action Pool: {self._resource_pool.name} - {self._command}"