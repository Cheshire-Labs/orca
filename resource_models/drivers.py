from dataclasses import dataclass
import xml.etree.ElementTree as ET
import subprocess

from typing import Any, Dict, List, Optional
from resource_models.transporter_resource import TransporterResource

from resource_models.base_resource import IResource
from resource_models.loadable_resources.ilabware_loadable import LoadableEquipmentResource
from resource_models.labware import Labware
from resource_models.loadable_resources.location import Location


class PlaceHolderNonLabwareResource(IResource):
    def __init__(self, name: str, mocking_type: Optional[str] = None):
        self._name = name
        self._mocking_type = mocking_type
        self._init_options: Dict[str, Any] = {}
    
    @property
    def name(self) -> str:
        return self._name
    
    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._init_options = init_options



# TODO: Just a placehodler for now
class PlaceHolderResource(LoadableEquipmentResource):
    def __init__(self, name: str, mocking_type: Optional[str] = None):
        super().__init__(name)
        self._mocking_type = mocking_type

    def initialize(self) -> bool:
        print(f"Initializing MockResource")
        print(f"Name: {self._name}")
        print(f"Type: {self._mocking_type}")
        print(f"Mock Initialized")
        self._is_initialized = True
        return self._is_initialized

    def set_command(self, command: str) -> None:
        self._command = command
    
    def set_command_options(self, options: Dict[str, Any]) -> None:
        self._command_options = options

    def load_labware(self, labware: Labware) -> None:
        self._is_running = True
        print(f"{self._name} open plate door")
        self._is_running = False

    def unload_labware(self, labware: Labware) -> None:
        self._is_running = True
        print(f"{self._name} close plate door")
        self._is_running = False

    def is_running(self) -> bool:
        return self._is_running

    def execute(self) -> None:
        self._is_running = True
        print(f"{self._name} execute")
        self._is_running = False

# TODO: Just a place holder for now 
class PlaceHolderRoboticArm(TransporterResource):
    def __init__(self, name: str, mocking_type: Optional[str] = None) -> None:
        super().__init__(name)
        self._name = name
        self._mocking_type = mocking_type
        self._plate_type: Optional[str] = None
        self._positions: List[str] = []
        self._command: Optional[str] = None

    def initialize(self) -> bool:
        print(f"Initializing MockRoboticArm")
        print(f"Name: {self._name}")
        print(f"Type: {self._mocking_type}")
        print(f"Mock Initialized")
        self._is_initialized = True
        return self._is_initialized

    def pick(self, location: Location) -> None:
        self._validate_position(location.name)
        print(f"{self._name} pick {self._plate_type} from {location}")
    
    def place(self, location: Location) -> None:
        self._validate_position(location.name)
        print(f"{self._name} place {self._plate_type} to {location}")

    def _validate_position(self, position: str) -> None:
        if position not in self._positions:
            raise ValueError(f"The position '{position}' is not taught for {self._name}")

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._init_options = init_options
        if "positions" in init_options.keys():
            self._positions = init_options["positions"]

    def get_taught_positions(self) -> List[str]:
        return self._positions
    

class VenusProtocol(LoadableEquipmentResource):
    def __init__(self, name: str):
        super().__init__(name)
        self._default_exe_path = r"C:\Program Files (x86)\HAMILTON\Bin\HxRun.exe"

    def initialize(self) -> bool:
        # add a simple hamilton initialization script here

        raise NotImplementedError()

    def load_labware(self, labware: Labware) -> None:
        print("Move carriage to load position")

    def unload_labware(self, labware: Labware) -> None:
        print("Move carriage to unload position")

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

