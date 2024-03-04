from typing import Optional, Dict, Any, Callable, List

from resource_models.transporter_resource import TransporterResource
from resource_models.labware import Labware
from resource_models.loadable_resources.ilabware_loadable import LoadableEquipmentResource
from resource_models.loadable_resources.location import Location

class MockEquipmentResource(LoadableEquipmentResource):
    def __init__(self, name: str, mocking_type: Optional[str] = None):
        super().__init__(name)
        self._mocking_type = mocking_type
        self._on_intialize: Callable[[Dict[str, Any]], None] = lambda x: None
        self._on_load_labware: Callable[[Labware], None] = lambda x: None
        self._on_unload_labware: Callable[[Labware], None] = lambda x: None
        self._on_execute: Callable[[], None] = lambda: None

    def set_command_options(self, options: Dict[str, Any]) -> None:
        self._command_options = options
    
    def set_command(self, command: str) -> None:
        self._command = command

    def initialize(self) -> bool:
        print(f"Initializing MockResource")
        print(f"Name: {self._name}")
        print(f"Type: {self._mocking_type}")
        print(f"Mock Initialized")
        self._is_initialized = True
        self._on_intialize(self._init_options)
        return self._is_initialized

    def load_labware(self, labware: Labware) -> None:
        self._is_running = True
        print(f"{self._name} open plate door")
        self._on_load_labware(labware)
        self._is_running = False

    def unload_labware(self, labware: Labware) -> None:
        self._is_running = True
        print(f"{self._name} close plate door")
        self._on_unload_labware(labware)
        self._is_running = False

    def is_running(self) -> bool:
        return self._is_running

    def execute(self) -> None:
        self._is_running = True
        print(f"{self._name} execute")
        self._on_execute()
        self._is_running = False

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
