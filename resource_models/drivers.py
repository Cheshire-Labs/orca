import subprocess

from typing import Any, Dict, List, Optional
from resource_models.location import Location
from resource_models.transporter_resource import TransporterResource

from resource_models.base_resource import BaseResource, Equipment, LabwareLoadable

from resource_models.labware import Labware


class PlaceHolderNonLabwareResource(BaseResource):
    def __init__(self, name: str, mocking_type: Optional[str] = None):
        self._name = name
        self._mocking_type = mocking_type
        self._init_options: Dict[str, Any] = {}
        self._is_initialized = False
    
    @property
    def is_initialized(self) -> bool:
        return self._is_initialized
    
    @property
    def name(self) -> str:
        return self._name
    
    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._init_options = init_options

    def initialize(self) -> None:
        self._is_initialized = True

class PlaceHolderResource(Equipment, LabwareLoadable):
    def __init__(self, name: str, mocking_type: Optional[str] = None):
        super().__init__(name)
        self._mocking_type = mocking_type
        self._stage_labware: Optional[Labware] = None
        self._loaded_labware: List[Labware] = []

    @property
    def labware(self) -> Optional[Labware]:
        return self._stage_labware

    def initialize(self) -> None:
        print(f"Initializing MockResource")
        print(f"Name: {self._name}")
        print(f"Type: {self._mocking_type}")
        print(f"Mock Initialized")
        self._is_initialized = True


    def prepare_for_place(self, labware: Labware) -> None:
        if self._stage_labware is not None:
            raise ValueError(f"{self} - Stage already contains labware: {self._stage_labware}.  Unable to place {labware}")
        self._is_running = True
        print(f"{self._name} open plate door")
        self._is_running = False

    def prepare_for_pick(self, labware: Labware) -> None:
        if self._stage_labware == labware:
            return
        else:
            self._is_running = True
            self.unload_labware(labware)
            self._is_running = False

    def unload_labware(self, labware: Labware) -> None:
        if labware not in self._loaded_labware:
            raise ValueError(f"{self} - Labware {labware} not found in loaded labwares")
        if self._stage_labware is not None:
            raise ValueError(f"{self} - Stage already contains labware: {self._stage_labware}.  Unable to unload {labware}")
        self._loaded_labware.remove(labware)
        print(f"{self} - labware {labware} unloaded")
        self._stage_labware = labware

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

    def is_running(self) -> bool:
        return self._is_running

    def execute(self) -> None:
        if self._command is None:
            raise ValueError(f"{self} - No command to execute")
        self._is_running = True
        print(f"{self} - execute - {self._command}")
        self._command = None
        self._options = {}
        self._is_running = False

class PlaceHolderRoboticArm(TransporterResource):
    def __init__(self, name: str, mocking_type: Optional[str] = None) -> None:
        super().__init__(name)
        self._name = name
        self._mocking_type = mocking_type
        self._plate_type: Optional[str] = None
        self._positions: List[str] = []
        self._command: Optional[str] = None

    def initialize(self) -> None:
        print(f"Initializing MockResource")
        print(f"Name: {self._name}")
        print(f"Type: {self._mocking_type}")
        print(f"Mock Initialized")
        if "teachpoints" in self._init_options:
            self._positions = self._init_options["teachpoints"]
        self._is_initialized = True


    def pick(self, location: Location) -> None:
        self._validate_position(location.teachpoint_name)
        if self._labware is not None:
            raise ValueError(f"{self} already contains labware: {self._labware}")
        if location.labware is None:
            raise ValueError(f"{location} does not contain labware")
        print(f"{self._name} pick {location.labware} from {location}")
        self._labware = location.labware
    
    def place(self, location: Location) -> None:
        self._validate_position(location.teachpoint_name)
        if self._labware is None:
            raise ValueError(f"{self} does not contain labware")
        if location.labware is not None:
            raise ValueError(f"{location} already contains labware")
        print(f"{self._name} place {self._labware} to {location}")
        self._labware = None

    def _validate_position(self, position: str) -> None:
        if position not in self._positions:
            raise ValueError(f"The position '{position}' is not taught for {self._name}")

    def get_taught_positions(self) -> List[str]:
        return self._positions
    

class VenusProtocol(BaseResource, LabwareLoadable):
    def __init__(self, name: str):
        super().__init__(name)
        self._default_exe_path = r"C:\Program Files (x86)\HAMILTON\Bin\HxRun.exe"
        self._locked = False

    def initialize(self) -> None:
        # add a simple hamilton initialization script here

        raise NotImplementedError()

    def prepare_for_place(self, labware: Labware) -> None:
        print("Move carriage to load position")
        self._locked = True

    def prepare_for_pick(self, labware: Labware) -> None:
        print("Move carriage to unload position")
        self._locked = True

    def notify_picked(self, labware: Labware) -> None:
        self._locked = False

    def notify_placed(self, labware: Labware) -> None:
        self._locked = False

    def set_command(self, command: str) -> None:
        self._command = command
    
    def set_command_options(self, options: Dict[str, Any]) -> None:
        self._options = options

    def is_running(self) -> bool:
        return self._is_running

    def execute(self) -> None:
        if self._command == 'EXECUTE':
            self._execute_protocol()
        else:
            raise NotImplementedError(f"The action '{self._command}' is unknown for {self._name} of type {type(self).__name__}")

    def _execute_protocol(self) -> None:

        if 'hxrun_path' in self._options.keys():
            exe_path = self._options['hxrun_path']
        else:
            exe_path = self._default_exe_path
        if 'method' not in self._options.keys():
            raise KeyError("The venus method was ")
        method = self._options["method"]
        try:
            self._is_running = True
            subprocess.run([exe_path, "-t", method], shell=True, check=True)
        except subprocess.CalledProcessError as e:
            print(f"Error: {e}")
        finally:
            self._is_running = False

