import uuid
from resource_models.base_resource import Equipment
from resource_models.labware import AnyLabware, Labware, LabwareTemplate
from resource_models.location import Location
from resource_models.resource_pool import EquipmentResourcePool
from routing.system_graph import SystemGraph
from workflow_models.action import ActionStatus, BaseAction


from abc import ABC
from typing import Any, Dict, List, Union


# class ILabwareMatcher(ABC):
#     def is_match(self, labware: Labware) -> bool:
#         raise NotImplementedError

# class LabwareToTemplateMatcher(ILabwareMatcher):
#     def __init__(self, match_templates: List[LabwareTemplate]) -> None:
#         self.match_templates = match_templates

#     def is_match(self, labware: Labware) -> bool:
#         for template in self.match_templates:
#             if labware.name == template.name and labware.labware_type == template.labware_type:
#                 return True
#         return False
    
# class LabwareInstanceMatcher(ILabwareMatcher):
#     def __init__(self, match_lawbare: List[Labware]) -> None:
#         self._match_lawbare = match_lawbare

#     def is_match(self, labware: Labware) -> bool:
#         return labware in self._match_lawbare
    
# class AnyLabwareMatcher(ILabwareMatcher):
#     def is_match(self, labware: Labware) -> bool:
#         return True

# class LabwareInputManager:
#     def __init__(self, labware_matcher: List[ILabwareMatcher]) -> None:
#         self._awaiting_input_matches: List[ILabwareMatcher] = labware_matcher
#         self._loaded_labware: Dict[Labware, ILabwareMatcher] = {}

#     def is_valid(self, labware: Labware) -> bool:
#         for matcher in self._awaiting_input_matches:
#             if matcher.is_match(labware):
#                 return True
#         return False

#     def load_labware(self, labware: Labware) -> None:
#         # check all the Non-AnyLabwareMatchers first to see if this labware can fill them
#         for matcher in self._awaiting_input_matches:
#             if not isinstance(matcher, AnyLabwareMatcher):
#                 if matcher.is_match(labware):
#                     self._awaiting_input_matches.remove(matcher)
#                     self._loaded_labware[labware] = matcher
#                     return
#         # if no non-any matchers are filled, check the any matchers
#         for matcher in self._awaiting_input_matches:
#             if isinstance(matcher, AnyLabwareMatcher):
#                 self._awaiting_input_matches.remove(matcher)
#                 self._loaded_labware[labware] = matcher
#                 return
#         raise ValueError(f"Labware {labware.name} is not accepted by this input manager")

# class MethodAction(BaseAction):

#     def __init__(self,
#                  resource: Equipment,
#                  command: str,
#                  input_manager: LabwareInputManager,
#                  output_matchers: List[ILabwareMatcher],
#                  options: Dict[str, Any] = {}) -> None:
#         self._resource: Equipment = resource
#         self._command: str = command
#         self._options: Dict[str, Any] = options
#         self._input_manager: LabwareInputManager = input_manager
#         # self._awaiting_labware_inputs: List[Labware] = labware_instance_inputs
#         # self._loaded_labware_inputs: List[Labware] = []
#         self._outputs: List[ILabwareMatcher] = output_matchers
#         self._status: ActionStatus = ActionStatus.CREATED

#     @property
#     def resource(self) -> Equipment:
#         return self._resource


#     def _perform_action(self) -> None:
#         self._resource.set_command(self._command)
#         self._resource.set_command_options(self._options)
#         self._status = ActionStatus.IN_PROGRESS
#         self._resource.execute()
#         self._status = ActionStatus.COMPLETED

#     def __str__(self) -> str:
#         return f"Method Action: {self._resource.name} - {self._command}"



class LocationAction(BaseAction):
    def __init__(self,
                 location: Location,
                 command: str,
                 expected_inputs: List[Union[Labware, AnyLabware]],
                 expected_outputs: List[Labware],
                 options: Dict[str, Any] = {}) -> None:
        self._location = location
        self._command: str = command
        self._options: Dict[str, Any] = options
        self._expected_inputs = expected_inputs
        self._expected_outputs = expected_outputs

    @property
    def resource(self) -> Equipment | None:
        return self._location.resource
    
    @property
    def location(self) -> Location:
        return self._location

    # def prepare_for_pick(self, labware: Labware) -> None:

    # def prepare_for_place(self, labware: Labware) -> None: 

    # def notify_picked(self, labware: Labware) -> None:
    
    # def notify_placed(self, labware: Labware) -> None:
    #     self._input_manager.load_labware(self._input_manager._loaded_labware)
    #     self._resource.load_labware(self._input_manager._loaded_labware)

    def _perform_action(self) -> None:
        # # Check that all the labware has arrived
        # if self._input_manager.is_awaiting_labware():
        #     raise ValueError("Not all the labware has arrived")


        # Execute the action
        if self.resource is not None:
            self.resource.set_command(self._command)
            self.resource.set_command_options(self._options)
            self._status = ActionStatus.IN_PROGRESS
            self.resource.execute()

        # Set the labware as available to pickup
        # self._resource.unload_labware()
        # self._input_manager.unload_labware()

        # Set status
        self._status = ActionStatus.COMPLETED

    def __str__(self) -> str:
        return f"Location Action: {self.location} - {self._command}"
    
class DynamicResourceAction:
    def __init__(self,
                 resource: EquipmentResourcePool | List[Equipment] | Equipment,
                 command: str,
                 expected_inputs: List[Union[Labware, AnyLabware]],
                 expected_outputs: List[Labware],
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

    @property
    def resource_pool(self) -> EquipmentResourcePool:
        return self._resource_pool
    
    @property
    def expected_inputs(self) -> List[Union[Labware, AnyLabware]]:
        return self._expected_inputs
    
    @property
    def expected_outputs(self) -> List[Labware]:
        return self._expected_outputs


    def resolve_resource_action(self, reference_point: Location, system_graph: SystemGraph) -> LocationAction:
        resource = self._resource_pool.get_closest_available_resource(reference_point, system_graph)
        location = system_graph.get_resource_location(resource.name)
        return LocationAction(location,
                              self._command,
                              self._expected_inputs,
                              self._expected_outputs,
                              self._options)

    def __str__(self) -> str:
        return f"Resource Action Pool: {self._resource_pool.name} - {self._command}"
