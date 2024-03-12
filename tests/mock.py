from typing import Optional, Dict, Any, Callable, List
from resource_models.drivers import PlaceHolderResource, PlaceHolderRoboticArm
from resource_models.location import Location
from resource_models.labware import Labware


class MockEquipmentResource(PlaceHolderResource):
    def __init__(self, name: str, mocking_type: Optional[str] = None):
        super().__init__(name, mocking_type)
        self._on_intialize: Callable[[Dict[str, Any]], None] = lambda x: None
        self._on_load_labware: Callable[[Labware], None] = lambda x: None
        self._on_unload_labware: Callable[[Labware], None] = lambda x: None
        self._on_execute: Callable[[str], None] = lambda x: None

    def initialize(self) -> None:
        super().initialize()
        self._on_intialize(self._init_options)
    
    def load_labware(self, labware: Labware) -> None:
        super().load_labware(labware)
        self._on_load_labware(labware)
        

    def unload_labware(self, labware: Labware) -> None:
        super().unload_labware(labware)
        self._on_unload_labware(labware)

    def execute(self) -> None:
        if self._command is None:
            raise ValueError(f"{self} - No command to execute")
        command = self._command
        super().execute()
        self._on_execute(command)


class MockRoboticArm(PlaceHolderRoboticArm):
    def __init__(self, name: str, mocking_type: Optional[str] = None) -> None:
        super().__init__(name, mocking_type)
        self._on_pick: Callable[[Labware, Location], None] = lambda x, y: None
        self._on_place: Callable[[Labware, Location], None] = lambda x, y: None

    def pick(self, location: Location) -> None:
        super().pick(location)
        if self._labware is None: 
            raise ValueError(f"{self} does not contain labware.  Base class pick method did not raise error")
        self._on_pick(self._labware, location)
        
    
    def place(self, location: Location) -> None:
        if self._labware is None:
            raise ValueError(f"{self} does not contain labware to place")
        labware = self._labware
        super().place(location)
        self._on_place(labware, location)
        

    def set_taught_positions(self, positions: List[str]) -> None:
        self._positions = positions

