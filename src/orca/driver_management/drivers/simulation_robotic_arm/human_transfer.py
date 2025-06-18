import logging
from typing import Any, Dict, List


from orca.driver_management.drivers.simulation_base.simulation_base import SimulationBaseDriver
from orca.resource_models.resource_extras.teachpoints import Teachpoint
from orca_driver_interface.transporter_interfaces import ITransporterDriver

orca_logger = logging.getLogger("orca")

class HumanTransferDriver(ITransporterDriver):
    def __init__(self, 
                 name: str, 
                 teachpoints_filepath: str | None = None, 
                 sim_time: float = 0.2
                 ) -> None:
        self._name = name
        self._teachpoints_filepath = teachpoints_filepath
        self._positions: List[str] = []
        simulation_driver = SimulationBaseDriver(name, mocking_type="Human Driver", sim_time=sim_time)

    @property
    def is_initialized(self) -> bool:
        return True
    
    @property
    def is_connected(self) -> bool:
        return True
    
    async def connect(self) -> None:
        return None
    
    async def disconnect(self) -> None:
        return None
    
    @property
    def name(self) -> str:
        return self._name
    
    @property
    def is_running(self) -> bool:
        return True
    
    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        return None

    async def initialize(self) -> None:
        self._is_initialized = True
        self._load_taught_positions()

    async def pick(self, position_name: str, labware_type: str) -> None:
        self._validate_position(position_name)
        orca_logger.info(f"Driver: {self._name} picking from {position_name}, labware type: {labware_type} picking...")
        input("Press Enter once the labware is picked up...")
        orca_logger.info(f"Driver: {self._name} picked from {position_name}, labware type: {labware_type} picked")

    async def place(self, position_name: str, labware_type: str) -> None:
        self._validate_position(position_name)
        orca_logger.info(f"Driver: {self._name} placing to {position_name}, labware type: {labware_type} placing...")
        input("Press Enter once the labware is placed...")
        orca_logger.info(f"Driver: {self._name} placed to {position_name}, labware type: {labware_type} placed")

    def _validate_position(self, position_name: str) -> None:
        if position_name not in self._positions:
            raise ValueError(f"The position '{position_name}' is not taught for {self._name}")

    def get_taught_positions(self) -> List[str]:
        return self._positions

    def _load_taught_positions(self) -> None:
        if self._teachpoints_filepath:
            self._positions = [t.name for t in Teachpoint.load_teachpoints_from_file(self._teachpoints_filepath)]
        else:
            raise ValueError(f"Teachpoints file path is not provided for {self._name}. Please provide a valid file path to load taught positions.")     
