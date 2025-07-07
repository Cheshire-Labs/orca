
import uuid
from orca.resource_models.devices import Device
from orca.resource_models.labware import AnyLabwareTemplate, LabwareInstance, LabwareTemplate
from orca.resource_models.location import Location
from orca.resource_models.resource_pool import ResourcePool

from typing import Any, Callable, Dict, List, Optional, Union

from orca.system.reservation_manager.interfaces import IThreadReservationCoordinator
from orca.system.system_map import SystemMap
from orca.workflow_models.actions.location_action import LocationAction
from orca.workflow_models.actions.util import AssignedLabwareManager, ResourcePoolResolver


class UnresolvedLocationAction:
    def __init__(self, 
                 resource: ResourcePool | List[Device] | Device, 
                 location_action: LocationAction,
                 expected_input_templates: List[Union[LabwareTemplate, AnyLabwareTemplate]], 
                 expected_output_templates: List[Union[LabwareTemplate, AnyLabwareTemplate]], 
                 options: Optional[Dict[str, Any]] = None) -> None:
        self._id = str(uuid.uuid4())
        self._resource_pool: ResourcePool
        if isinstance(resource, ResourcePool):
            self._resource_pool = resource
        elif isinstance(resource, list):
            self._resource_pool = ResourcePool(
                f"Generated Resource Pool - {uuid.uuid4()}",
                [equip for equip in resource if isinstance(equip, Device)]
            )
        elif isinstance(resource, Device):
            self._resource_pool = ResourcePool(f"Generated Resource Pool - {uuid.uuid4()}", [resource])
        self._location_action = location_action
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
    def resource_pool(self) -> ResourcePool:
        return self._resource_pool
    
    @property
    def expected_input_templates(self) -> List[LabwareTemplate | AnyLabwareTemplate]:
        return self._expected_input_templates
    
    @property
    def expected_output_templates(self) -> List[LabwareTemplate | AnyLabwareTemplate]:
        return self._expected_output_templates
    
    @property
    def expected_inputs(self) -> List[LabwareInstance]:
        return self._assigned_labware_manager.expected_inputs
    
    @property
    def expected_outputs(self) -> List[LabwareInstance]:
        return self._assigned_labware_manager.expected_outputs
       
    def assign_input(self, template_slot: LabwareTemplate, input: LabwareInstance):
        self._assigned_labware_manager.assign_input(template_slot, input)

    def get_location_action(self) -> LocationAction:
        """
        Returns a LocationAction instance with the assigned labware manager.
        This method is used to create the action after the labware has been assigned.
        """
        self._location_action.set_assigned_labware_manager(self._assigned_labware_manager)
        return self._location_action
    
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
        location_action = dynamic_action.get_location_action()
        location_action.set_location_reservation(location_reservation)
        return location_action
            
