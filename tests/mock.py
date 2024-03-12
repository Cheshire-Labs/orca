from typing import Optional, Dict, Any, Callable, List
from resource_models.base_resource import Equipment, LabwareLoadable
from resource_models.location import Location
from resource_models.resource_extras.teachpoints import Teachpoint

from resource_models.transporter_resource import TransporterResource
from resource_models.labware import Labware


class MockEquipmentResource(Equipment, LabwareLoadable):
    def __init__(self, name: str, mocking_type: Optional[str] = None):
        super().__init__(name)
        self._mocking_type = mocking_type
        self._stage_labware: Optional[Labware] = None
        self._loaded_labware: List[Labware] = []

        self._on_intialize: Callable[[Dict[str, Any]], None] = lambda x: None
        self._on_load_labware: Callable[[Labware], None] = lambda x: None
        self._on_unload_labware: Callable[[Labware], None] = lambda x: None
        self._on_execute: Callable[[str], None] = lambda x: None


    @property
    def labware(self) -> Optional[Labware]:
        return self._stage_labware

    def initialize(self) -> None:
        print(f"Initializing MockResource")
        print(f"Name: {self._name}")
        print(f"Type: {self._mocking_type}")
        print(f"Mock Initialized")
        self._is_initialized = True
        self._on_intialize(self._init_options)

    def prepare_for_place(self, labware: Labware) -> None:
        if self._stage_labware != None:
            raise ValueError(f"{self} - Stage already contains labware: {self._stage_labware}.  Unable to place {labware}")

    def notify_picked(self, labware: Labware) -> None:
        if self._stage_labware != labware:
            raise ValueError(f"{self} - An error has ocurred.  The labware {labware} notified as picked does not match the previously staged labware. "
                              "The wrong labware may have been picked.")
        self._stage_labware = None
    
    def notify_placed(self, labware: Labware) -> None:
        if self._stage_labware is not None:
            raise ValueError(f"{self} - An error has ocurred.  The labware {labware} notified as placed was placed with labware {self._stage_labware} already on the stage.  "
                             "A crash may have occurred.")
        self._stage_labware = labware
        print(f"{self} - labware {labware} received on stage")
        self.load_labware(labware)
    
    def load_labware(self, labware: Labware) -> None:
        if self._stage_labware != labware:
            raise ValueError(f"{self} - Stage labware {self._stage_labware} does not match labware {labware} to load")
        self._stage_labware = None
        self._loaded_labware.append(labware)
        print(f"{self} - labware {labware} loaded")
        self._on_load_labware(labware)
        
    def prepare_for_pick(self, labware: Labware) -> None:
        self.unload_labware(labware)

    def unload_labware(self, labware: Labware) -> None:
        if labware not in self._loaded_labware:
            raise ValueError(f"{self} - Labware {labware} not found in loaded labwares")
        if self._stage_labware is not None:
            raise ValueError(f"{self} - Stage already contains labware: {self._stage_labware}.  Unable to unload {labware}")
        self._loaded_labware.remove(labware)
        print(f"{self} - labware {labware} unloaded")
        self._stage_labware = labware
        self._on_unload_labware(labware)
        
        print(f"{self._name} - labware {labware} unloaded")

    # def stage_labware(self, labware: Labware) -> None:
    #     if self._stage_labware is not None:
    #         raise ValueError(f"{self} - Stage already contains labware: {self._stage_labware}")
    #     self._stage_labware = labware
    #     print(f"{self._name} - labware {labware} staged")

    # def unstage_labware(self, labware: Labware) -> None:
    #     if self._stage_labware != labware:
    #         raise ValueError(f"{self} - Stage labware {self._stage_labware} does not match labware {labware} to unstage")
    #     self._stage_labware = None
    #     print(f"{self._name} - labware {labware} unstaged")

    def execute(self) -> None:
        print(f"{self} - execute - {self._command}")
        if self._command is None:
            raise ValueError(f"{self} - No command to execute")
        self._on_execute(self._command)
        self._command = None
        self._options = {}



class MockRoboticArm(TransporterResource):
    def __init__(self, name: str, mocking_type: Optional[str] = None) -> None:
        super().__init__(name)
        self._mocking_type = mocking_type
        self._labware: Optional[Labware] = None
        self._positions: List[str] = []
        self._on_pick: Callable[[Labware, Location], None] = lambda x, y: None
        self._on_place: Callable[[Labware, Location], None] = lambda x, y: None

    def initialize(self) -> None:
        print(f"Initializing MockResource")
        print(f"Name: {self._name}")
        print(f"Type: {self._mocking_type}")
        print(f"Mock Initialized")
        if "teachpoints" in self._init_options:
            self._positions = self._init_options["teachpoints"]
        self._is_initialized = True

    def pick(self, location: Location) -> None:
        if self._labware is not None:
            raise ValueError(f"{self} already contains labware: {self._labware}")
        if location.labware is None:
            raise ValueError(f"{location} does not contain labware")
        print(f"{self._name} pick {location.labware} from {location}")
        self._labware = location.labware
        self._on_pick(self._labware, location)
        
    
    def place(self, location: Location) -> None:
        if self._labware is None:
            raise ValueError(f"{self} does not contain labware")
        if location.labware is not None:
            raise ValueError(f"{location} already contains labware")
        print(f"{self._name} place {self._labware} to {location}")
        self._on_place(self._labware, location)
        self._labware = None

    def get_taught_positions(self) -> List[str]:
        return self._positions

    def set_taught_positions(self, positions: List[str]) -> None:
        self._positions = positions

    def _load_teachpoints(self, teachpoints: List[str] | str) -> None:
        if isinstance(teachpoints, str):
            if ".xml" in teachpoints:
                self._positions = [t.name for t in Teachpoint.load_teachpoints_from_file(teachpoints)]
        else:
            self._positions = teachpoints

