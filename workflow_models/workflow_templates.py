
from abc import ABC
from typing import Any, Dict, List, Optional
from resource_models.equipment_resource import EquipmentResource
from resource_models.labware import LabwareTemplate
from resource_models.location import Location
from workflow_models.action import BaseAction


class MethodActionTemplate(BaseAction, ABC):
    def __init__(self, resource: EquipmentResource, command: str, options: Optional[Dict[str, Any]] = None, inputs: Optional[List[LabwareTemplate]] = None, outputs: Optional[List[LabwareTemplate]] = None):
        self._resource = resource
        self._command = command
        self._options: Dict[str, Any] = {} if options is None else options
        self._inputs: List[LabwareTemplate] = inputs if inputs is not None else []
        self._outputs: List[LabwareTemplate] = outputs if outputs is not None else []

    @property
    def resource(self) -> EquipmentResource:
        return self._resource
    @property
    def inputs(self) -> List[LabwareTemplate]:
        return self._inputs
    
    @property
    def outputs(self) -> List[LabwareTemplate]:
        return self._outputs

    @property
    def command(self) -> str:
        return self._command
    
    @property
    def options(self) -> Dict[str, Any]:
        return self._options

class MethodTemplate:

    def __init__(self, name: str):
        self._name = name
        self._actions: List[MethodActionTemplate] = []

    @property
    def name(self) -> str:
        return self._name
    

    @property
    def actions(self) -> List[MethodActionTemplate]:
        return self._actions

    def append_action(self, action: MethodActionTemplate):
        self._actions.append(action)

class LabwareThreadTemplate:

    def __init__(self, labware: LabwareTemplate, start: Location, end: Location) -> None:
        self._labware: LabwareTemplate = labware
        self._start: Location = start
        self._end: Location = end
        self._methods: List[MethodTemplate] = []

    @property
    def labware(self) -> LabwareTemplate:
        return self._labware
    
    @property
    def start(self) -> Location:
        return self._start
    
    @property
    def end(self) -> Location:
        return self._end
    
    @property
    def methods(self) -> List[MethodTemplate]:
        return self._methods
    
    def add_method(self, method: MethodTemplate, method_step_options: Optional[Dict[str, Any]] = None) -> None:
        # TODO: may add option to update method options at the Workflow level
        self._methods.append(method)


class WorkflowTemplate:
    
    def __init__(self, name: str) -> None:
        self._name = name
        self._labware_thread: Dict[str, LabwareThreadTemplate] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def labware_threads(self) -> Dict[str, LabwareThreadTemplate]:
        return self._labware_thread
