
import uuid
from orca.resource_models.base_resource import Equipment
from orca.resource_models.labware import AnyLabwareTemplate, Labware, LabwareTemplate
from orca.resource_models.location import Location
from orca.resource_models.resource_pool import EquipmentResourcePool
from orca.system.labware_registry_interfaces import ILabwareRegistry

from typing import Any, Dict, List, Set, Union

from orca.system.reservation_manager import IReservationManager
from orca.system.system_map import IResourceLocator, SystemMap
from orca.workflow_models.action import AssignedLabwareManager, IActionObserver, LocationAction, LocationActionCollectionReservationRequest
from orca.workflow_models.status_enums import ActionStatus


class DynamicResourceAction:
    def __init__(self,
                 labware_registry: ILabwareRegistry,
                 resource: EquipmentResourcePool | List[Equipment] | Equipment,
                 command: str,
                 expected_input_templates: List[Union[LabwareTemplate, AnyLabwareTemplate]],
                 expected_output_templates: List[Union[LabwareTemplate, AnyLabwareTemplate]],
                 options: Dict[str, Any] = {}) -> None:
        if isinstance(resource, EquipmentResourcePool):
            self._resource_pool: EquipmentResourcePool = resource
        elif isinstance(resource, list):
            self._resource_pool = EquipmentResourcePool(f"Generated Resource Pool - {uuid.uuid4()}", resource)
        elif isinstance(resource, Equipment):
            self._resource_pool = EquipmentResourcePool(f"Generated Resource Pool - {uuid.uuid4()}", [resource])
        self._command: str = command
        self._options: Dict[str, Any] = options
        self._assigned_labware_manager = AssignedLabwareManager(labware_registry, 
                                                                                  expected_input_templates, 
                                                                                  expected_output_templates)
        self._location_action: LocationAction | None = None
        self._observers: List[IActionObserver] = []

    @property
    def command(self) -> str:
        return self._command
    
    @property
    def options(self) -> Dict[str, Any]:
        return self._options

    @property
    def resource_pool(self) -> EquipmentResourcePool:
        return self._resource_pool
    
    @property
    def expected_input_templates(self) -> List[Union[LabwareTemplate, AnyLabwareTemplate]]:
        return self._assigned_labware_manager.expected_input_templates
    
    @property
    def expected_output_templates(self) -> List[Union[LabwareTemplate, AnyLabwareTemplate]]:
        return self._assigned_labware_manager.expected_output_templates

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
        if self._location_action is None:
            return ActionStatus.AWAITING_LOCATION_RESERVATION
        return self._location_action.status

    def add_observer(self, observer: IActionObserver) -> None:
        if observer not in self._observers:
            self._observers.append(observer)
        if self._location_action is not None:
            self._location_action.add_observer(observer)

    async def resolve_action(self, reference_point: Location, reservation_manager: IReservationManager, system_map: SystemMap) -> LocationAction:
        if self._location_action is not None:
            return self._location_action
        reservation_request = LocationActionCollectionReservationRequest(self._get_potential_location_actions(system_map))
        self._location_action = await reservation_request.reserve_location(reservation_manager, reference_point, system_map)
        for observer in self._observers:
            self._location_action.add_observer(observer)
        self._location_action._status = ActionStatus.RESOLVED
        return self._location_action

    def _get_potential_location_actions(self, resource_locator: IResourceLocator) -> List[LocationAction]:
        potential_locations: Set[Location] = set()
        for resource in self._resource_pool.resources:
            potential_location = resource_locator.get_resource_location(resource.name)
            potential_locations.add(potential_location)

        location_actions: List[LocationAction] = []
        for location in potential_locations:
            location_action = LocationAction(location, 
                                             self._command, 
                                             self._assigned_labware_manager,
                                             self._options)
            location_actions.append(location_action)
        return location_actions
    
    
    def __str__(self) -> str:
        return f"Resource Action Pool: {self._resource_pool.name} - {self._command}"



            

