from abc import ABC, abstractmethod
import subprocess

import time
from typing import Any, Dict, List, Optional
from resource_models.resource_extras.teachpoints import Teachpoint

class IInitializableDriver(ABC):
    @property
    @abstractmethod
    def is_initialized(self) -> bool:
        raise NotImplementedError
    
    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        raise NotImplementedError
    
    @abstractmethod
    async def initialize(self) -> None:
        raise NotImplementedError
    

class IDriver(IInitializableDriver, ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError
    
    @property
    @abstractmethod
    def is_running(self) -> bool:
        raise NotImplementedError
    
    @abstractmethod
    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        raise NotImplementedError
    
class ILabwarePlaceableDriver(IDriver, ABC):
    
    @abstractmethod
    async def prepare_for_pick(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        raise NotImplementedError
    
    @abstractmethod
    async def prepare_for_place(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        raise NotImplementedError

    @abstractmethod
    async def notify_picked(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        raise NotImplementedError
    
    @abstractmethod
    async def notify_placed(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        raise NotImplementedError


class ITransporterDriver(IDriver, ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError
    
    @property
    @abstractmethod
    def is_running(self) -> bool:
        raise NotImplementedError
    
    @abstractmethod
    async def pick(self, position_name: str, labware_type: str) -> None:
        raise NotImplementedError
    
    @abstractmethod
    async def place(self, position_name: str, labware_type: str) -> None:
        raise NotImplementedError
    
    @abstractmethod
    def get_taught_positions(self) -> List[str]:
        raise NotImplementedError   
    

class SimulationBaseDriver(IDriver):
    def __init__(self, name: str, mocking_type: Optional[str] = None, sim_time: float = 0.2):
        self._name: str = name
        self._mocking_type = mocking_type
        self._init_options: Dict[str, Any] = {}
        self._is_initialized: bool = False
        self._is_running: bool = False
        self._sim_time = sim_time

    @property
    def name(self) -> str:
        return self._name
    
    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    @property
    def is_running(self) -> bool:
        return self._is_running
    
    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._init_options = init_options

    async def initialize(self) -> None:
        self._sleep()
        self._is_initialized = True

    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        print(f"{self._name} executing command: {command}")
        if len(options.keys()) > 0:
            print(f"Options: {options}")
        self._sleep()
        print(f"{self._name} executed command: {command}")

    def _sleep(self) -> None:
        self._is_running = True
        time.sleep(self._sim_time)
        self._is_running = False


class SimulationDriver(SimulationBaseDriver, ILabwarePlaceableDriver):      

    async def prepare_for_pick(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        print(f"Driver: {self._name} preparing for pick...")
        self._sleep()
        print(f"Driver: {self._name} prepared for pick")

    async def prepare_for_place(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        print(f"Driver: {self._name} preparing for place...")
        self._sleep()
        print(f"Driver: {self._name} prepared for place")

    async def notify_picked(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        print(f"Driver: {self._name} notified picked...")
        self._sleep()
        print(f"Driver: {self._name} notified picked")

    async def notify_placed(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        print(f"Driver: {self._name} notified placed...")
        self._sleep()
        print(f"Driver: {self._name} notified placed")


class SimulationRoboticArm(SimulationBaseDriver, ITransporterDriver):
    def __init__(self, name: str, mocking_type: Optional[str] = None, sim_time: float = 0.2) -> None:
        super().__init__(name, mocking_type, sim_time)
        self._positions: List[str] = []

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        super().set_init_options(init_options)
        self._load_taught_positions(self._init_options)

    async def initialize(self) -> None:
        self._sleep()
        self._is_initialized = True
    
    async def pick(self, position_name: str, labware_type: str) -> None:
        self._validate_position(position_name)
        print(f"Driver: {self._name} picking from {position_name}, labware type: {labware_type} picking...")
        self._sleep()
        print(f"Driver: {self._name} picked from {position_name}, labware type: {labware_type} picked")
    
    async def place(self, position_name: str, labware_type: str) -> None:
        self._validate_position(position_name)
        print(f"Driver: {self._name} placing to {position_name}, labware type: {labware_type} placing...")
        self._sleep()
        print(f"Driver: {self._name} placed to {position_name}, labware type: {labware_type} placed")

    def _validate_position(self, position_name: str) -> None:
        if position_name not in self._positions:
            raise ValueError(f"The position '{position_name}' is not taught for {self._name}")

    def get_taught_positions(self) -> List[str]:
        return self._positions
    
    def _load_taught_positions(self, init_options: Dict[str, Any]) -> None:
        if "positions" in init_options:
            positions_config = init_options["positions"]
            if isinstance(positions_config, list):
                self._positions = positions_config
            elif isinstance(positions_config, str):
                self._positions = [t.name for t in Teachpoint.load_teachpoints_from_file(positions_config)]
            else:
                raise ValueError(f"Positions configuration for {self._name} is not recognized.  Must be a filepath string or list of strings naming the teachpoints")

class NullPlatePadDriver(ILabwarePlaceableDriver):
    def __init__(self, name: str) -> None:
        self._name = name
        self._is_initialized = False
        self._init_options: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return self._name
    
    @property
    def is_initialized(self) -> bool:
        return self._is_initialized
    
    @property
    def is_running(self) -> bool:
        return False

    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        pass

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._init_options = init_options
    
    async def initialize(self) -> None:
        self._is_initialized = True
    
    async def prepare_for_pick(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        pass
    
    async def prepare_for_place(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        pass
    
    async def notify_picked(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        pass
    
    async def notify_placed(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        pass
    
    def __str__(self) -> str:
        return f"PlatePad: {self._name}"
    
class VenusProtocolDriver(ILabwarePlaceableDriver):

    def __init__(self, name: str):
        self._name = name
        self._exe_path = r"C:\Program Files (x86)\HAMILTON\Bin\HxRun.exe"
        self._is_initialized = False
        self._is_running = False
        self._init_protocol = "init.hsl"

    @property
    def name(self) -> str:
        return self._name
    
    @property
    def is_initialized(self) -> bool:
        return self._is_initialized
    
    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._init_options = init_options

    async def initialize(self) -> None:
        if "init-protocol" in self._init_options.keys():
            self._init_protocol = self._init_options["init-protocol"]
        if "hxrun-path" in self._init_options.keys():
            self._exe_path = self._init_options["hxrun-path"]
        # add a simple hamilton initialization script here
        await self.execute("run", {"method": self._init_protocol})
        self._is_initialized = True

    @property
    def is_running(self) -> bool:
        return self._is_running
    
    async def prepare_for_place(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        print("Move carriage to load position")
        self._locked = True

    async def prepare_for_pick(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        print("Move carriage to unload position")
        self._locked = True

    async def notify_picked(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        self._locked = False

    async def notify_placed(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        self._locked = False
    
    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        if command == "run":
            if 'method' not in options.keys():
                raise KeyError("The venus method was ")
            method = options["method"]
            self._execute_protocol(method)
        else:
            raise NotImplementedError(f"The action '{command}' is unknown for {self._name} of type {type(self).__name__}")

    def _execute_protocol(self, hsl_path: str) -> None:
        try:
            self._is_running = True
            subprocess.run([self._exe_path, "-t", hsl_path], shell=True, check=True)
        except subprocess.CalledProcessError as e:
            print(f"Error: {e}")
        finally:
            self._is_running = False

