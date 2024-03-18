from resource_models.base_resource import Equipment
from resource_models.labware import Labware, LabwareTemplate
from resource_models.location import Location
from resource_models.resource_pool import EquipmentResourcePool
from routing.system_graph import SystemGraph
from workflow_models.action import ActionStatus, BaseAction


from abc import ABC
from typing import Any, Dict, List, Optional


class ILabwareMatcher(ABC):
    def is_match(self, labware: Labware) -> bool:
        raise NotImplementedError

class LabwareToTemplateMatcher(ILabwareMatcher):
    def __init__(self, match_templates: List[LabwareTemplate]) -> None:
        self.match_templates = match_templates

    def is_match(self, labware: Labware) -> bool:
        for template in self.match_templates:
            if labware.name == template.name and labware.labware_type == template.labware_type:
                return True
        return False
    
class LabwareInstanceMatcher(ILabwareMatcher):
    def __init__(self, match_lawbare: List[Labware]) -> None:
        self._match_lawbare = match_lawbare

    def is_match(self, labware: Labware) -> bool:
        return labware in self._match_lawbare
    
class AnyLabwareMatcher(ILabwareMatcher):
    def is_match(self, labware: Labware) -> bool:
        return True

class LabwareInputManager:
    def __init__(self, labware_matcher: List[ILabwareMatcher]) -> None:
        self._awaiting_input_matches: List[ILabwareMatcher] = labware_matcher
        self._loaded_labware: Dict[Labware, ILabwareMatcher] = {}

    def is_valid(self, labware: Labware) -> bool:
        for matcher in self._awaiting_input_matches:
            if matcher.is_match(labware):
                return True
        return False

    def load_labware(self, labware: Labware) -> None:
        # check all the Non-AnyLabwareMatchers first to see if this labware can fill them
        for matcher in self._awaiting_input_matches:
            if not isinstance(matcher, AnyLabwareMatcher):
                if matcher.is_match(labware):
                    self._awaiting_input_matches.remove(matcher)
                    self._loaded_labware[labware] = matcher
                    return
        # if no non-any matchers are filled, check the any matchers
        for matcher in self._awaiting_input_matches:
            if isinstance(matcher, AnyLabwareMatcher):
                self._awaiting_input_matches.remove(matcher)
                self._loaded_labware[labware] = matcher
                return
        raise ValueError(f"Labware {labware.name} is not accepted by this input manager")

class MethodAction(BaseAction):

    def __init__(self,
                 resource: Equipment,
                 command: str,
                 input_manager: LabwareInputManager,
                 output_matchers: List[ILabwareMatcher],
                 options: Dict[str, Any] = {}) -> None:
        self._resource: Equipment = resource
        self._command: str = command
        self._options: Dict[str, Any] = options
        self._input_manager: LabwareInputManager = input_manager
        # self._awaiting_labware_inputs: List[Labware] = labware_instance_inputs
        # self._loaded_labware_inputs: List[Labware] = []
        self._outputs: List[ILabwareMatcher] = output_matchers
        self._status: ActionStatus = ActionStatus.CREATED

    @property
    def resource(self) -> Equipment:
        return self._resource


    def _perform_action(self) -> None:
        self._resource.set_command(self._command)
        self._resource.set_command_options(self._options)
        self._status = ActionStatus.IN_PROGRESS
        self._resource.execute()
        self._status = ActionStatus.COMPLETED

    def __str__(self) -> str:
        return f"Method Action: {self._resource.name} - {self._command}"


class MethodActionResolver(BaseAction):
    def __init__(self,
                 resource_pool: EquipmentResourcePool,
                 command: str,
                 input_manager: LabwareInputManager,
                 labware_instance_outputs: List[ILabwareMatcher],
                 options: Dict[str, Any] = {}) -> None:
        self._resource_pool: EquipmentResourcePool = resource_pool
        self._command: str = command
        self._options: Dict[str, Any] = options
        self._input_manager = input_manager
        self._outputs: List[ILabwareMatcher] = labware_instance_outputs

    @property
    def resource_pool(self) -> EquipmentResourcePool:
        return self._resource_pool

    def get_best_action(self, sourcing_location: Location, system: SystemGraph) -> MethodAction:
        available_resources = [resource for resource in self._resource_pool.resources if resource.is_available]
        ordered_resources = sorted(available_resources, key=lambda x: system.get_distance(sourcing_location.teachpoint_name, system.get_resource_location(x.name).teachpoint_name))
        if not ordered_resources:
            raise ValueError(f"{self}: No available resources")
        return MethodAction(ordered_resources[0],
                            self._command,
                            self._input_manager, 
                            self._outputs, 
                            self._options)

    def __str__(self) -> str:
        return f"Method Action Resolver: {self._resource_pool.name} - {self._command}"