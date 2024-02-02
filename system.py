from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from drivers.base_resource import IResource
from drivers.drivers import ILabwareTransporter
from drivers.resource_factory import ResourceFactory
from labware import Labware
from location import Location
from method import Action, Method
from workflow import LabwareThread, Workflow

class System:
    @property
    def options(self) -> Dict[str, Any]:
        return self._options
    
    @property
    def labwares(self) -> Dict[str, Labware]:
        return self._labwares

    @labwares.setter
    def labwares(self, value: Dict[str, Labware]) -> None:
        self._labwares = value

    @property
    def resources(self) -> Dict[str, IResource]:
        return self._resources
    
    @resources.setter
    def resources(self, value: Dict[str, IResource]) -> None:
        self._resources = value

    @property
    def locations(self) -> Dict[str, Location]:
        return self._locations
    
    @locations.setter
    def locations(self, value: Dict[str, Location]) -> None:
        self._locations = value

    @property 
    def methods(self) -> Dict[str, Method]:
        return self._methods
    
    @methods.setter
    def methods(self, value: Dict[str, Method]) -> None:
        self._methods = value

    @property
    def workflows(self) -> Dict[str, Workflow]:
        return self._workflows
    
    @workflows.setter
    def workflows(self, value: Dict[str, Workflow]) -> None:
        self._workflows = value
    
    @property
    def labware_transporters(self) -> Dict[str, ILabwareTransporter]:
        return {name: r for name, r in self._resources.items() if isinstance(r, ILabwareTransporter)}

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
