from typing import Dict, Any
from resource_models.transporter_resource import TransporterResource
from resource_models.base_resource import IResource
from resource_models.base_resource import BaseResource
from resource_models.loadable_resources.ilabware_loadable import LoadableEquipmentResource
from resource_models.labware import Labware
from resource_models.loadable_resources.location import Location
from routing.system_graph import SystemGraph
from workflow_models.workflow import Method, Workflow
class System:
    def __init__(self,
                 name: str,
                 description: str,
                 version: str,
                 options: Dict[str, Any],
                 labwares: Dict[str, Labware],
                 resources: Dict[str, IResource],
                 locations: Dict[str, Location],
                 methods: Dict[str, Method],
                 workflows: Dict[str, Workflow]) -> None:
        self._name = name
        self._description = description
        self._version = version 
        self._options = options
        self._labwares: Dict[str, Labware] = labwares
        self._resources: Dict[str, IResource] = resources
        self._locations: Dict[str, Location] = locations
        self._methods: Dict[str, Method] = methods
        self._workflows: Dict[str, Workflow] = workflows
        self._system_graph: SystemGraph = self._build_system_graph()
    
    @property
    def options(self) -> Dict[str, Any]:
        return self._options
    
    @property
    def labwares(self) -> Dict[str, Labware]:
        return self._labwares
    
    @property
    def resources(self) -> Dict[str, IResource]:
        return self._resources
    
    @property
    def equipment(self) -> Dict[str, LoadableEquipmentResource]:
        return {name: r for name, r in self._resources.items() if isinstance(r, LoadableEquipmentResource)}
   
    @property
    def labware_transporters(self) -> Dict[str, TransporterResource]:
        return {name: r for name, r in self._resources.items() if isinstance(r, TransporterResource)}

    @property
    def locations(self) -> Dict[str, Location]:
        return self._locations
    
    @property 
    def methods(self) -> Dict[str, Method]:
        return self._methods   
    
    @property
    def workflows(self) -> Dict[str, Workflow]:
        return self._workflows
    
    @property
    def system_graph(self) -> SystemGraph:
        return self._system_graph
    
    def _build_system_graph(self) -> SystemGraph:
        graph = SystemGraph()
        for location in self._locations.values():
            graph.add_location(location)
        return graph
