from typing import Optional, Dict, Any, Callable, List
from resource_models.location import Location

from resource_models.transporter_resource import TransporterResource
from resource_models.labware import Labware
from resource_models.base_resource import BaseLabwareableResource

class MockEquipmentResource(BaseLabwareableResource):
    def __init__(self, name: str, mocking_type: Optional[str] = None):
        super().__init__(name)
        self._mocking_type = mocking_type
        self._stage_labware: Optional[Labware] = None
        self._loaded_labware: List[Labware] = []

        self._on_intialize: Callable[[Dict[str, Any]], None] = lambda x: None
        self._on_load_labware: Callable[[Labware], None] = lambda x: None
        self._on_unload_labware: Callable[[Labware], None] = lambda x: None
        self._on_execute: Callable[[], None] = lambda: None

    def initialize(self) -> bool:
        print(f"Initializing MockResource")
        print(f"Name: {self._name}")
        print(f"Type: {self._mocking_type}")
        print(f"Mock Initialized")
        self._is_initialized = True
        self._on_intialize(self._init_options)
        return self._is_initialized

    def stage_labware(self, labware: Labware) -> None:
        if self._stage_labware is not None:
            raise ValueError(f"Stage already contains labware: {self._stage_labware}")
        self._stage_labware = labware
        print(f"{self._name} - labware {labware} staged")

    def unstage_labware(self, labware: Labware) -> None:
        if self._stage_labware != labware:
            raise ValueError(f"Stage labware {self._stage_labware} does not match labware {labware} to unstage")
        self._stage_labware = None
        print(f"{self._name} - labware {labware} unstaged")

    def load_labware(self, labware: Labware) -> None:
        if labware != self._stage_labware:
            raise ValueError(f"Labware {labware} not on stage.  Stage contains labware {self._stage_labware}")
        self._on_load_labware(labware)
        print(f"{self._name} - labware {labware} loaded from stage")

    def unload_labware(self, labware: Labware) -> None:
        if labware not in self._loaded_labware:
            raise ValueError(f"Labware {labware} not found in loaded labwares")
        self._loaded_labware.remove(labware)
        print(f"{self._name} - labware {self.labware} unloaded to stage")
        self._stage_labware = labware

    def execute(self) -> None:
        print(f"{self._name} execute")
        self._on_execute()

    def set_on_intialize(self, on_intialize: Callable[[Dict[str, Any]], None]) -> None:
        self._on_intialize = on_intialize
    
    def set_on_load_labware(self, on_load_labware: Callable[[Labware], None]) -> None:
        self._on_load_labware = on_load_labware
    
    def set_on_unload_labware(self, on_unload_labware: Callable[[Labware], None]) -> None:
        self._on_unload_labware = on_unload_labware

    def set_on_execute(self, on_execute: Callable[[], None]) -> None:
        self._on_execute = on_execute

class MockRoboticArm(TransporterResource):
    def __init__(self, name: str, mocking_type: Optional[str] = None) -> None:
        super().__init__(name)
        self._mocking_type = mocking_type
        self._labware: Optional[Labware] = None
        self._positions: List[str] = []
        self._on_pick: Callable[[Labware, Location], None] = lambda x, y: None
        self._on_place: Callable[[Labware, Location], None] = lambda x, y: None

    def initialize(self) -> bool:
        print(f"Initializing MockResource")
        print(f"Name: {self._name}")
        print(f"Type: {self._mocking_type}")
        print(f"Mock Initialized")
        self._is_initialized = True
        return self._is_initialized

    def pick(self, location: Location) -> None:
        if self._labware is not None:
            raise ValueError(f"Robot already contains labware: {self._labware}")
        if location.labware is None:
            raise ValueError(f"Location {location} does not contain labware")
        self._labware = location.labware
        self._on_pick(self._labware, location)
        print(f"{self._name} pick {self._labware} from {location}")
    
    def place(self, location: Location) -> None:
        if self._labware is None:
            raise ValueError(f"Robot does not contain labware")
        if location.labware is not None:
            raise ValueError(f"Location {location} already contains labware")
        self._on_place(self._labware, location)
        print(f"{self._name} place {self._labware} to {location}")

    def get_taught_positions(self) -> List[str]:
        return self._positions

    def set_taught_positions(self, positions: List[str]) -> None:
        self._positions = positions

    def set_on_pick(self, on_pick: Callable[[Labware, Location], None]) -> None:
        self._on_pick = on_pick
    
    def set_on_place(self, on_place: Callable[[Labware, Location], None]) -> None:
        self._on_place = on_place
