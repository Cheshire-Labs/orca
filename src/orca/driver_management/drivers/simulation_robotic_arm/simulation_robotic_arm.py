import logging
from typing import List, Optional


from orca.driver_management.drivers.simulation_base.simulation_base import SimulationBaseDriver
from orca.resource_models.resource_extras.teachpoints import Teachpoint
from orca_driver_interface.transporter_interfaces import ITransporterDriver

orca_logger = logging.getLogger("orca")

class SimulationRoboticArmDriver(SimulationBaseDriver, ITransporterDriver):
    """ A simulation driver for a robotic arm that can pick and place labware."""
    def __init__(self, 
                 name: str, 
                 mocking_type: Optional[str] = None, 
                 teachpoints: str | List[str] | None = None, 
                 sim_time: float = 0.2
                 ) -> None:
        """ Initializes the SimulationRoboticArmDriver with a name, mocking type, and optional teachpoints.
        Args:
            name (str): The name of the driver.
            mocking_type (Optional[str]): The type of equipment this simulation is mocking, e.g., "robotic_arm".
            teachpoints (str | List[str] | None): The teachpoints to use for the robotic arm, can be a file path or a list of position names.
            sim_time (float): The time to simulate for each operation, default is 0.2 seconds.
        """
        super().__init__(name, mocking_type, sim_time)
        self._positions: List[str] = []
        self.set_teachpoints(teachpoints if teachpoints is not None else [])


    async def initialize(self) -> None:
        self._sleep()
        self._is_initialized = True

    async def pick(self, position_name: str, labware_type: str) -> None:
        self._validate_position(position_name)
        orca_logger.info(f"Driver: {self._name} picking from {position_name}, labware type: {labware_type} picking...")
        self._sleep()
        orca_logger.info(f"Driver: {self._name} picked from {position_name}, labware type: {labware_type} picked")

    async def place(self, position_name: str, labware_type: str) -> None:
        self._validate_position(position_name)
        orca_logger.info(f"Driver: {self._name} placing to {position_name}, labware type: {labware_type} placing...")
        self._sleep()
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
  