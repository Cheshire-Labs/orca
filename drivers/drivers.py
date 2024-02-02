from abc import abstractmethod
from dataclasses import dataclass
import xml.etree.ElementTree as ET
import subprocess

from typing import Any, Dict, List, Optional

from drivers.base_resource import BaseResource, IResource

class ILabwareTransporter(IResource):

    @abstractmethod
    def pick(self) -> None:
        raise NotImplementedError
    
    @abstractmethod
    def place(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def set_plate_type(self, plate_type: str) -> None:
        raise NotImplementedError
    
    @abstractmethod
    def get_accessible_locations(self) -> List[str]:
        raise NotImplementedError


class MockResource(BaseResource):
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

    def load_plate(self) -> None:
        self._is_running = True
        print(f"{self._name} open plate door")
        self._is_running = False

    def unload_plate(self) -> None:
        self._is_running = True
        print(f"{self._name} close plate door")
        self._is_running = False

    def is_running(self) -> bool:
        return self._is_running

    def execute(self) -> None:
        self._is_running = True
        print("f{self.resource_name} execute")
        self._is_running = False

@dataclass
class TeachPoint:
    name: str
    wrist: float
    elbow: float
    shoulder: float


class MockRoboticArm(MockResource, ILabwareTransporter):
    def __init__(self, name: str, mocking_type: Optional[str] = None) -> None:
        super().__init__(name, mocking_type)
        self._plate_type: Optional[str] = None
        self._teachpoint_file: Optional[str] = None
        self._teachpoints: List[TeachPoint] = []

    def pick(self) -> None:
        self._options["plate"]
        raise NotImplementedError
    
    def place(self) -> None:
        raise NotImplementedError
    
    def set_plate_type(self, plate_type: str) -> None:
        self._plate_type = plate_type
    
    def _load_teachpoint_file(self, xml_file: str) -> None:
        self._teachpoints = []

        # Parse the XML file
        tree = ET.parse(xml_file)
        root = tree.getroot()

        # Iterate through <teachpoint> elements
        for teachpoint_elem in root.findall(".//teachpoint"):
            name = teachpoint_elem.get("name")
            wrist = teachpoint_elem.get("wrist", 0.0) 
            elbow = teachpoint_elem.get("elbow", 0.0)
            shoulder = teachpoint_elem.get("shoulder", 0.0)

            teachpoint = TeachPoint(str(name), float(wrist), float(elbow), float(shoulder))
            self._teachpoints.append(teachpoint)

    def get_accessible_locations(self) -> List[str]:
        return [t.name for t in self._teachpoints]
    
    def execute(self) -> None:
        if self._command == 'PICK':
            self.load_plate()
        elif self._command == 'PLACE':
            self.unload_plate()
        else:
            raise NotImplementedError(f"The action '{self._command}' is unknown for {self._name} of type {type(self).__name__}")
    

class VenusProtocol(BaseResource):
    def __init__(self, name: str):
        super().__init__(name)
        self._default_exe_path = r"C:\Program Files (x86)\HAMILTON\Bin\HxRun.exe"

    def initialize(self) -> bool:
        # add a simple hamilton initialization script here

        raise NotImplementedError()

    def load_plate(self) -> None:
        print("Move carriage to load position")

    def unload_plate(self) -> None:
        print("Move carriage to unload position")

    def execute(self) -> None:
        if self._command == 'EXECUTE':
            self._execute_protocol()
        elif self._command == 'LOAD':
            self.load_plate()
        elif self._command == 'UNLOAD':
            self.unload_plate()
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

