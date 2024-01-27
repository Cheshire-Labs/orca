from typing import Dict, List
from equipment_resource import IResource
from labware import Labware

from main import ActionCommand, IRoboticArm


class Router:
    def __init__(self) -> None:
        self._robots: List[IRoboticArm] = []
        self._labwares: List[Labware] = []
        self._resource_location_map: Dict[str, str] = {}
    
    def get_next_move(self) -> ActionCommand:
        # collect all the different labwares
        pass 

class Location:
    def __init__(self) -> None:
        self._name = name
        self._is_available = True
        self._resource = None
        self._can_resolve_deadlock = True

    def set_resource(self, resource: IResource) -> None:
        self._resource = resource

class SystemLocations:
    def __init__(self) -> None:
        self._locations = {}

    def add(location: Location) -> None:
        self._locations.append(location)



class WorkflowRoute:
    def __init__(self) -> None:
        self._labware: Labware = None
        self._path: Dict[int, Location] = {}
