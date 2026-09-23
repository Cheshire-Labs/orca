
import uuid
from orca.resource_models.devices import Device
from orca.resource_models.labware import AnyLabwareTemplate, LabwareInstance, LabwareTemplate
from orca.resource_models.location import Location
from orca.resource_models.resource_pool import ResourcePool
from orca.config import ReservationConfig
from orca.state.records import DeclaredTracking
from orca.resource_models.well_selector import WellSelector

from typing import Any, Dict, List, Optional, Union

from orca.system.reservation_manager.interfaces import IThreadReservationCoordinator
from orca.system.system_map import SystemMap
from orca.workflow_models.actions.assigned_location_action import AssignedLocationAction
from orca.workflow_models.actions.location_action import ActionBodyLocationAction
from orca.workflow_models.actions.util import AssignedLabwareManager, IActionReservationStatusSink, ResourcePoolResolver
from orca.workflow_models.status_enums import FailurePolicy


class UnresolvedLocationAction:
    def __init__(self,
                 resource: ResourcePool | List[Device] | Device,
                 location_action: ActionBodyLocationAction,
                 expected_input_templates: List[Union[LabwareTemplate, AnyLabwareTemplate]],
                 expected_output_templates: List[Union[LabwareTemplate, AnyLabwareTemplate]],
                 options: Optional[Dict[str, Any]] = None,
                 failure_policy: FailurePolicy = FailurePolicy.PAUSE,
                 deck_positions: Optional[Dict[LabwareTemplate, str]] = None,
                 well_selectors: Optional[Dict[str, WellSelector]] = None,
                 declares: Optional[DeclaredTracking] = None,
                 tag: Optional[str] = None) -> None:
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
        self._failure_policy = failure_policy
        self._deck_positions: Dict[LabwareTemplate, str] = deck_positions or {}
        self._well_selectors: Dict[str, WellSelector] = well_selectors or {}
        self._declares = declares
        self._tag: str | None = tag
        self._was_skipped: bool = False
        self._assigned: AssignedLocationAction | None = None

    @property
    def id(self) -> str:
        return self._id

    @property
    def was_skipped(self) -> bool:
        return self._was_skipped

    def mark_skipped(self) -> None:
        self._was_skipped = True

    @property
    def command(self) -> str:
        return self._location_action.command

    @property
    def tag(self) -> str | None:
        """Optional anchor tag from the source ActionTemplate. Only tagged
        actions can be targeted by Before/After anchors in mutation."""
        return self._tag

    @property
    def failure_policy(self) -> FailurePolicy:
        return self._failure_policy

    @property
    def resource_pool(self) -> ResourcePool:
        return self._resource_pool
    
    @property
    def deck_positions(self) -> Dict[LabwareTemplate, str]:
        return self._deck_positions

    @property
    def well_selectors(self) -> Dict[str, WellSelector]:
        return self._well_selectors

    @property
    def declares(self) -> DeclaredTracking | None:
        return self._declares

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
       
    def assign_input(self, template_slot: LabwareTemplate, input: LabwareInstance) -> None:
        self._assigned_labware_manager.assign_input(template_slot, input)

    def try_assign_labware(
        self,
        labware_template: LabwareTemplate,
        labware: LabwareInstance,
    ) -> None:
        """Assign labware if this action expects it (exact match or AnyLabwareTemplate)."""
        if labware_template in self._expected_input_templates:
            self.assign_input(labware_template, labware)
        elif any(isinstance(t, AnyLabwareTemplate) for t in self._expected_input_templates):
            self.assign_input(labware_template, labware)

    def is_input_assigned(self, template: LabwareTemplate | AnyLabwareTemplate) -> bool:
        return self._assigned_labware_manager.is_input_assigned(template)

    def assign(self) -> AssignedLocationAction:
        """Freeze the labware manager onto the action and hand off the
        AssignedLocationAction stage. Idempotent: one AssignedLocationAction
        per unresolved lifetime, so retries reuse the same assigned stage
        (and mint fresh executables from it) rather than re-freezing."""
        if self._assigned is None:
            self._assigned_labware_manager.freeze()
            self._location_action.set_assigned_labware_manager(self._assigned_labware_manager)
            self._assigned = AssignedLocationAction(self._location_action)
        return self._assigned
    
class DynamicResourceActionResolver:
    def __init__(self, reservation_coordinator: IThreadReservationCoordinator, system_map: SystemMap,
                 reservation_config: ReservationConfig | None = None) -> None:
        self._reservation_coordinator = reservation_coordinator
        self._system_map = system_map
        self._reservation_config = reservation_config or ReservationConfig()

    def get_resource_location(self, resource_name: str) -> Location:
        """Look up the location of a named resource."""
        return self._system_map.get_resource_location(resource_name)

    def get_potential_locations(self, action: UnresolvedLocationAction) -> set[Location]:
        """Return possible physical locations for an unresolved action's resource pool."""
        locations: set[Location] = set()
        for resource in action.resource_pool.resources:
            if isinstance(resource, Device) and resource.locations:
                locations.update(resource.locations)
            else:
                locations.add(self._system_map.get_resource_location(resource.name))
        return locations

    async def resolve_action(
        self,
        thread_id: str,
        dynamic_action: UnresolvedLocationAction,
        reference_point: Location,
        requesting_labware: LabwareInstance | None = None,
        status_sink: IActionReservationStatusSink | None = None,
    ) -> AssignedLocationAction:
        resolver = ResourcePoolResolver(dynamic_action.resource_pool, self._reservation_config)
        location_reservation = await resolver.resolve_action_location(
            thread_id,
            reference_point,
            self._reservation_coordinator,
            self._system_map,
            requesting_labware=requesting_labware,
            status_sink=status_sink,
        )
        assigned = dynamic_action.assign()
        location_action = assigned.location_action
        location_action.set_location_reservation(location_reservation)

        reserved_location = location_reservation.reserved_location
        for resource in dynamic_action.resource_pool.resources:
            if isinstance(resource, Device) and reserved_location in resource.locations:
                location_action.set_device(resource)
                break

        if dynamic_action.well_selectors:
            location_action.set_well_selectors(dynamic_action.well_selectors)
        if dynamic_action.declares is not None:
            location_action.set_declares(dynamic_action.declares)

        return assigned
            
