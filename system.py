from typing import Dict, Any, Optional
from resource_models.location import Location
from resource_models.transporter_resource import TransporterResource
from resource_models.base_resource import IResource, LabwareLoadable

from resource_models.labware import Labware
from routing.system_graph import SystemGraph
from workflow_models.workflow import Workflow

class System:
    def __init__(self,
                 name: str,
                 description: str,
                 version: str,
                 options: Dict[str, Any],
                 resources: Dict[str, IResource],
                 locations: Dict[str, Location]) -> None:
        self._name = name
        self._description = description
        self._version = version 
        self._options = options
        self._labwares: Dict[str, Labware] = {}
        self._resources: Dict[str, IResource] = resources
        self._locations: Dict[str, Location] = locations
        self._workflow: Optional[Dict[str, Workflow]] = None
        self._system_graph: SystemGraph = self._build_system_graph()
    
    @property
    def options(self) -> Dict[str, Any]:
        return self._options
    
    @property
    def labwares(self) -> Dict[str, Labware]:
        return self._labwares
    
    def add_labware(self, labware: Labware) -> None:
        self._labwares[labware.name] = labware
    
    @property
    def resources(self) -> Dict[str, IResource]:
        return self._resources
    
    @property
    def equipment(self) -> Dict[str, LabwareLoadable]:
        return {name: r for name, r in self._resources.items() if isinstance(r, LabwareLoadable)}
   
    @property
    def labware_transporters(self) -> Dict[str, TransporterResource]:
        return {name: r for name, r in self._resources.items() if isinstance(r, TransporterResource)}

    @property
    def locations(self) -> Dict[str, Location]:
        return self._locations
    
    @property
    def workflows(self) -> Dict[str, Workflow]:
        if self._workflows is None:
            raise ValueError("Workflows have not been set")
        return self._workflows
    
    @workflows.setter
    def workflows(self, workflows: Dict[str, Workflow]) -> None:
        self._workflows = workflows
    
    @property
    def system_graph(self) -> SystemGraph:
        return self._system_graph
    
    def _build_system_graph(self) -> SystemGraph:
        graph = SystemGraph()
        for location in self._locations.values():
            graph.add_location(location)
        return graph
