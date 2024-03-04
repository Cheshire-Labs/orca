from typing import Any, Dict, Optional
from resource_models.transporter_resource import TransporterResource
from resource_models.base_resource import IResource
from resource_models.base_resource import BaseResource
from resource_models.loadable_resources.ilabware_loadable import LoadableEquipmentResource

from resource_models.labware import Labware, LabwareTemplate
from resource_models.loadable_resources.location import Location
from system import System
from workflow_models.workflow import Method, Workflow

from workflow_models.workflow_templates import MethodTemplate, WorkflowTemplate


class SystemTemplate:
    def __init__(self, 
                name: str, 
                description: str = "", 
                version: str = "latest", 
                options: Dict[str, Any] = {}
                ) -> None:
        self._name = name
        self._description = description
        self._version = version 
        self._options = options
        self._labwares: Dict[str, LabwareTemplate] = {}
        self._resources: Dict[str, IResource] = {}
        self._locations: Dict[str, Location] = {}
        self._methods: Dict[str, MethodTemplate] = {}
        self._workflows: Dict[str, WorkflowTemplate] = {}


    @property
    def options(self) -> Dict[str, Any]:
        return self._options
    
    @property
    def labwares(self) -> Dict[str, LabwareTemplate]:
        return self._labwares

    @labwares.setter
    def labwares(self, value: Dict[str, LabwareTemplate]) -> None:
        self._labwares = value

    @property
    def resources(self) -> Dict[str, IResource]:
        return self._resources
    
    @resources.setter
    def resources(self, value: Dict[str, IResource]) -> None:
        self._resources = value

    @property
    def equipment(self) -> Dict[str, LoadableEquipmentResource]:
        return {name: r for name, r in self._resources.items() if isinstance(r, LoadableEquipmentResource)}
   
    @property
    def labware_transporters(self) -> Dict[str, TransporterResource]:
        return {name: r for name, r in self._resources.items() if isinstance(r, TransporterResource)}

    @property
    def locations(self) -> Dict[str, Location]:
        return self._locations
    
    @locations.setter
    def locations(self, value: Dict[str, Location]) -> None:
        self._locations = value

    @property 
    def methods(self) -> Dict[str, MethodTemplate]:
        return self._methods
    
    @methods.setter
    def methods(self, value: Dict[str, MethodTemplate]) -> None:
        self._methods = value

    @property
    def workflows(self) -> Dict[str, WorkflowTemplate]:
        return self._workflows
    
    @workflows.setter
    def workflows(self, value: Dict[str, WorkflowTemplate]) -> None:
        self._workflows = value
    
    def create_system_instance(self) -> System:

        # TODO: figure out how to create the labware instances needed

        labwares = {name: Labware.from_template(labware) for name, labware in self._labwares.items()}
        workflows = {name: Workflow.from_template(workflow) for name, workflow in self._workflows.items()}
        methods = {name: Method.from_template(method) for name, method in self._methods.items()}
        
        return System(name=self._name,
                      description=self._description,
                      version=self._version,
                      options=self._options,
                      labwares=labwares,
                      resources=self._resources,
                      locations=self._locations,
                      methods=methods,
                      workflows=workflows)

