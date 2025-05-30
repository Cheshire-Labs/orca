
import uuid
from orca.resource_models.base_resource import Equipment
from orca.resource_models.labware import AnyLabwareTemplate, Labware, LabwareTemplate
from orca.resource_models.location import Location
from orca.resource_models.resource_pool import EquipmentResourcePool
from orca.sdk.events.execution_context import MethodExecutionContext
from orca.sdk.events.event_bus_interface import IEventBus
from orca.system.labware_registry_interfaces import ILabwareRegistry

from typing import Any, Dict, List, Set, Union

from orca.system.reservation_manager import IReservationManager
from orca.system.system_map import IResourceLocator, SystemMap
from orca.workflow_models.action import AssignedLabwareManager, LocationActionData, LocationActionExecutor, LocationActionCollectionReservationRequest
from orca.workflow_models.labware_thread import StatusManager
from orca.workflow_models.status_enums import ActionStatus


class ResourceActionData:
    def __init__(self, 
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
        self._command = command
        self._expected_input_templates = expected_input_templates
        self._expected_output_templates = expected_output_templates
        self._assigned_labware_manager = AssignedLabwareManager(
                                                    self._expected_input_templates, 
                                                    self._expected_output_templates)
        self._options = options

    
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
        return self._expected_input_templates
    
    @property
    def expected_output_templates(self) -> List[Union[LabwareTemplate, AnyLabwareTemplate]]:
        return self._expected_output_templates
    
    @property
    def expected_inputs(self) -> List[Labware]:
        return self._assigned_labware_manager.expected_inputs
    
    @property
    def expected_outputs(self) -> List[Labware]:
        return self._assigned_labware_manager.expected_outputs
       
    def assign_input(self, template_slot: LabwareTemplate, input: Labware):
        self._assigned_labware_manager.assign_input(template_slot, input)
    


class ResourceActionResolver:
    def __init__(self,
                status_manager: StatusManager,
                action: ResourceActionData) -> None:
        self._action = action

        self._location_action: LocationActionData | None = None
        self._status_manager = status_manager

    async def resolve_action_location(self, 
                             reference_point: Location, 
                             reservation_manager: IReservationManager, 
                             system_map: SystemMap, 
                             context: MethodExecutionContext) -> LocationActionData:
        if self._location_action is not None:
            return self._location_action
        reservation_request = LocationActionCollectionReservationRequest(self._get_potential_location_actions(system_map))
        self._location_action = await reservation_request.reserve_location(reservation_manager, reference_point, system_map)
        self._location_action._status = ActionStatus.RESOLVED
        return self._location_action

    def _get_potential_location_actions(self, resource_locator: IResourceLocator) -> List[LocationActionData]:
        potential_locations: Set[Location] = set()
        for resource in self._action.resource_pool.resources:
            potential_location = resource_locator.get_resource_location(resource.name)
            potential_locations.add(potential_location)

        location_actions: List[LocationActionData] = []
        for location in potential_locations:
            location_action = LocationActionData(location,
                                                 self._action.command,
                                             self._action.options)
            location_actions.append(location_action)
        return location_actions
    
    
    # def __str__(self) -> str:
    #     return f"Resource Action Pool: {self._action.resource_pool.name} - {self._action.command}"


    # @property
    # def status(self) -> ActionStatus:
    #     if self._location_action is None:
    #         return ActionStatus.AWAITING_LOCATION_RESERVATION
    #     return self._location_action.status

    # @status.setter
    # def status(self, status: ActionStatus) -> None:
    #     self._status_manager.set_status("ACTION", self._action.id, status, context=self._context)
            

