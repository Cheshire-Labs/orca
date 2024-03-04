from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from resource_models.base_resource import EquipmentResource, IResource, TransporterResource

from resource_models.labware import LabwareTemplate
from resource_models.location import Location
from routing.system_graph import SystemGraph
from workflow_models.workflow_templates import MethodTemplate, WorkflowTemplate

class SystemTemplate:
    def __init__(self, 
                name: Optional[str] = None, 
                description: Optional[str] = None, 
                version: Optional[str] = None, 
                options: Optional[Dict[str, Any]] = None
                ) -> None:
        self._name = name
        self._description = description
        self._version = version 
        self._options = options if options is not None else {}
        self._labwares: Dict[str, LabwareTemplate] = {}
        self._resources: Dict[str, IResource] = {}
        self._locations: Dict[str, Location] = {}
        self._methods: Dict[str, MethodTemplate] = {}
        self._workflows: Dict[str, WorkflowTemplate] = {}
        self._system_graph: SystemGraph = self._build_system_graph()

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
    def equipment_resources(self) -> Dict[str, EquipmentResource]:
        return {name: r for name, r in self._resources.items() if isinstance(r, EquipmentResource)}

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
    
    @property
    def labware_transporters(self) -> Dict[str, TransporterResource]:
        return {name: r for name, r in self._resources.items() if isinstance(r, TransporterResource)}

    @property
    def system_graph(self) -> SystemGraph:
        return self._system_graph
    
    def _build_system_graph(self) -> SystemGraph:
        graph = SystemGraph()
        for location in self._locations.values():
            graph.add_location(location)
        return graph