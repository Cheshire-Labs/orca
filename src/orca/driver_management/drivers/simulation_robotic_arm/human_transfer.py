import logging
from typing import Any, Dict, List


from orca.driver_management.drivers.simulation_base.simulation_base import SimulationBaseDriver
from orca.resource_models.resource_extras.teachpoints import Teachpoint
from orca_driver_interface.transporter_interfaces import ITransporterDriver

orca_logger = logging.getLogger("orca")

class HumanTransferDriver(ITransporterDriver):
    """ A driver that informs the user to pick and place labware manually."""
    def __init__(self, 
                 name: str, 
                 teachpoints: str | List[str] | None = None 
                 ) -> None:
        """ Initializes the HumanTransferDriver with a name and optional teachpoints.
        Args:
            name (str): The name of the driver.
            teachpoints (str | List[str] | None): The teachpoints to use for the robotic arm, can be a file path or a list of position names."""
        self._name = name
        self._positions: List[str] = []
        self.set_teachpoints(teachpoints if teachpoints is not None else [])

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

    async def pick(self, position_name: str, labware_type: str) -> None:
        self._validate_position(position_name)
        orca_logger.info(f"Driver: {self._name} picking from {position_name}, labware type: {labware_type} picking...")
        input(f"Press Enter once the labware {labware_type} is picked up from {position_name}...")
        orca_logger.info(f"Driver: {self._name} picked from {position_name}, labware type: {labware_type} picked")

    async def place(self, position_name: str, labware_type: str) -> None:
        self._validate_position(position_name)
        orca_logger.info(f"Driver: {self._name} placing to {position_name}, labware type: {labware_type} placing...")
        input(f"Press Enter once the labware {labware_type} is placed to {position_name}...")
        orca_logger.info(f"Driver: {self._name} placed to {position_name}, labware type: {labware_type} placed")

    def _validate_position(self, position_name: str) -> None:
        if position_name not in self._positions:
            raise ValueError(f"The position '{position_name}' is not taught for {self._name}")

    def get_taught_positions(self) -> List[str]:
        return self._positions

    def set_teachpoints(self, teachpoints: str | List[str]) -> None:
        if isinstance(teachpoints, str):
            self._positions = self._load_taught_positions(teachpoints)
        elif isinstance(teachpoints, list):
            self._positions = teachpoints

    def _load_taught_positions(self, filepath: str) -> List[str]:
        return [t.name for t in Teachpoint.load_teachpoints_from_file(filepath)]
   