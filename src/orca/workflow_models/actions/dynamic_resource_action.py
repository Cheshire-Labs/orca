
import uuid
from orca.resource_models.base_resource import Equipment
from orca.resource_models.labware import AnyLabwareTemplate, LabwareInstance, LabwareTemplate
from orca.resource_models.location import Location
from orca.resource_models.resource_pool import EquipmentResourcePool

from typing import Any, Dict, List, Optional, Union

from orca.system.reservation_manager.interfaces import IThreadReservationCoordinator
from orca.system.system_map import SystemMap
from orca.workflow_models.actions.location_action import LocationAction
from orca.workflow_models.actions.util import AssignedLabwareManager, ResourcePoolResolver


class UnresolvedLocationAction:
    def __init__(self, 
                 resource: EquipmentResourcePool | List[Equipment] | Equipment, 
                 command: str, 
                 expected_input_templates: List[Union[LabwareTemplate, AnyLabwareTemplate]], 
                 expected_output_templates: List[Union[LabwareTemplate, AnyLabwareTemplate]], 
                 options: Optional[Dict[str, Any]] = None) -> None:
        self._id = str(uuid.uuid4())
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
        self._options = options if options is not None else {}
    @property
    def id(self) -> str:
        return self._id
    
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
    def expected_inputs(self) -> List[LabwareInstance]:
        return self._assigned_labware_manager.expected_inputs
    
    @property
    def expected_outputs(self) -> List[LabwareInstance]:
        return self._assigned_labware_manager.expected_outputs
       
    def assign_input(self, template_slot: LabwareTemplate, input: LabwareInstance):
        self._assigned_labware_manager.assign_input(template_slot, input)
    
class DynamicResourceActionResolver:
    def __init__(self, reservation_coordinator: IThreadReservationCoordinator, system_map: SystemMap) -> None:
        self._reservation_coordinator = reservation_coordinator
        self._system_map = system_map

    async def resolve_action(self, thread_id: str, dynamic_action: UnresolvedLocationAction, reference_point: Location) -> LocationAction:
        resolver = ResourcePoolResolver(dynamic_action.resource_pool)
        location_reservation = await resolver.resolve_action_location(
            thread_id,
            reference_point,
            self._reservation_coordinator,
            self._system_map
        )
        location_action = LocationAction(
            location_reservation,
            dynamic_action._assigned_labware_manager,
            dynamic_action.command,
            dynamic_action.options
        )
        return location_action
            
