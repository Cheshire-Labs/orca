
import time
import logging
from typing import Any, Dict, List, Optional
from orca.drivers.driver_interfaces import IDriver, ILabwarePlaceableDriver
from orca.drivers.transporter_interfaces import ITransporterDriver
from orca.helper import FilepathReconciler
from orca.resource_models.resource_extras.teachpoints import Teachpoint

class SimulationBaseDriver(IDriver):
    def __init__(self, name: str, mocking_type: Optional[str] = None, sim_time: float = 0.2):
        self._name: str = name
        self._mocking_type = mocking_type
        self._init_options: Dict[str, Any] = {}
        self._is_initialized: bool = False
        self._is_running: bool = False
        self._connected: bool = False
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
    
    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
    
    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._init_options = init_options

    async def initialize(self) -> None:
        self._sleep()
        self._is_initialized = True

    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        logging.info(f"{self._name} executing command: {command}")
        if len(options.keys()) > 0:
            logging.info(f"Options: {options}")
        self._sleep()
        logging.info(f"{self._name} executed command: {command}")

    def _sleep(self) -> None:
        self._is_running = True
        time.sleep(self._sim_time)
        self._is_running = False


class SimulationDriver(SimulationBaseDriver, ILabwarePlaceableDriver):      

    async def prepare_for_pick(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        logging.info(f"Driver: {self._name} preparing for pick...")
        self._sleep()
        logging.info(f"Driver: {self._name} prepared for pick")

    async def prepare_for_place(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        logging.info(f"Driver: {self._name} preparing for place...")
        self._sleep()
        logging.info(f"Driver: {self._name} prepared for place")

    async def notify_picked(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        logging.info(f"Driver: {self._name} notified picked...")
        self._sleep()
        logging.info(f"Driver: {self._name} notified picked")

    async def notify_placed(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        logging.info(f"Driver: {self._name} notified placed...")
        self._sleep()
        logging.info(f"Driver: {self._name} notified placed")


class SimulationRoboticArmDriver(SimulationBaseDriver, ITransporterDriver):
    def __init__(self, name: str, filepath_reconciler: FilepathReconciler,  mocking_type: Optional[str] = None, sim_time: float = 0.2) -> None:
        super().__init__(name, mocking_type, sim_time)
        self._filepath_reconciler = filepath_reconciler
        self._positions: List[str] = []

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        super().set_init_options(init_options)
        self._load_taught_positions(self._init_options)

    async def initialize(self) -> None:
        self._sleep()
        self._is_initialized = True
    
    async def pick(self, position_name: str, labware_type: str) -> None:
        self._validate_position(position_name)
        logging.info(f"Driver: {self._name} picking from {position_name}, labware type: {labware_type} picking...")
        self._sleep()
        logging.info(f"Driver: {self._name} picked from {position_name}, labware type: {labware_type} picked")
    
    async def place(self, position_name: str, labware_type: str) -> None:
        self._validate_position(position_name)
        logging.info(f"Driver: {self._name} placing to {position_name}, labware type: {labware_type} placing...")
        self._sleep()
        logging.info(f"Driver: {self._name} placed to {position_name}, labware type: {labware_type} placed")

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
                positions_filepath = self._filepath_reconciler.reconcile_filepath(positions_config)
                self._positions = [t.name for t in Teachpoint.load_teachpoints_from_file(positions_filepath)]
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

    @property 
    def is_connected(self) -> bool:
        return True

    async def connect(self) -> None:
        pass
        
    async def disconnect(self) -> None:
        pass
    
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
    