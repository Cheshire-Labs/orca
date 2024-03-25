from __future__ import annotations

from typing import Dict, List, Set, Union
from resource_models.location import Location
from resource_models.labware import AnyLabware, Labware
from routing.router import Route
from system.system_map import SystemMap
from workflow_models.method_action import DynamicResourceAction, LocationAction
from workflow_models.method_status import MethodStatus



class Method:

    def __init__(self, name: str) -> None:
        self._name = name
        self._steps: List[DynamicResourceAction] = []
        self._status: MethodStatus = MethodStatus.CREATED
        self._children_threads: List[str] = []

    @property
    def name(self) -> str:
        return self._name
    
    @property
    def expected_inputs(self) -> List[Union[Labware, AnyLabware]]:
        # compile a set of the labware templates that are expected as inputs
        expected_inputs: Set[Union[Labware, AnyLabware]] = set()
        for action in self._steps:
            expected_inputs.update(action.expected_inputs)
        return list(expected_inputs)
    
    @property
    def expected_outputs(self) -> List[Labware]:
        # compile a set of the labware templates that are expected as outputs
        expected_outputs: Set[Labware] = set()
        for action in self._steps:
            expected_outputs.update(action.expected_outputs)
        return list(expected_outputs)
    
    def append_step(self, step: DynamicResourceAction) -> None:
        self._steps.append(step)

    def has_completed(self) -> bool:
        return self._status == MethodStatus.COMPLETED

    def resolve_next_action(self, reference_point: Location, system_map: SystemMap) -> LocationAction:
        self._status = MethodStatus.RUNNING
        # TODO: set children threads to spawn here

        dynamic_action = self._steps.pop(0)
        if len(self._steps) == 0:
            self._status = MethodStatus.COMPLETED
        return dynamic_action.resolve_resource_action(reference_point, system_map)

    def set_children_threads(self, thread_names: List[str]) -> None:
        self._children_threads = thread_names


       

class LabwareThread:

    def __init__(self, name: str, labware: Labware, start_location: Location, end_location: Location, system_map: SystemMap) -> None:
        self._name: str = name
        self._labware: Labware = labware
        self._start_location: Location = start_location
        self._current_location: Location = self._start_location
        self._end_location: Location = end_location
        self._route: Route | None = None
        self._system_map: SystemMap = system_map

    @property
    def name(self) -> str:
        return self._name

    @property
    def start_location(self) -> Location:
        return self._start_location
    
    @property
    def end_location(self) -> Location:
        if self._end_location is None:
            raise ValueError("End location has not been set")
        return self._end_location
    
    def set_end_location(self, location: Location) -> None:
        self._end_location = location
    
    @property
    def current_location(self) -> Location:
        return self._current_location

    @property
    def labware(self) -> Labware:
        return self._labware
    
    def initialize_labware(self) -> None:
        self._start_location.set_labware(self._labware)

    def execute_action(self, action: LocationAction) -> None:
        # TODO:  fix this

        self._route = Route(self._current_location, self._system_map , action.location)
        for step in self._route:
            step.set_labware(self._labware)
            step.execute()
            self._current_location = step.target
        action.execute()
    
    def send_to_end_location(self) -> None:
        # TODO:  fix this
        self._route = Route(self._current_location, self._system_map , self._end_location)
        for step in self._route:
            step.set_labware(self._labware)
            step.execute()
            self._current_location = step.target



class Workflow:
    # @staticmethod
    # def from_template(template: WorkflowTemplate, labwares: Dict[str, Labware]) -> Workflow:
    #     threads = {thread_name: LabwareThread.from_template(thread_template, labwares) for thread_name, thread_template in template.labware_threads.items()}
    #     workflow = Workflow(template.name, threads)
    #     return workflow

    def __init__(self, name:str, threads: Dict[str, LabwareThread]) -> None:
        self._name = name
        self._labware_threads:  Dict[str, LabwareThread] = threads

    @property
    def name(self) -> str:
        return self._name

    @property
    def labware_threads(self) ->  Dict[str, LabwareThread]:
        return self._labware_threads

