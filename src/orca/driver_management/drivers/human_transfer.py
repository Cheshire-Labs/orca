import asyncio
import logging
from typing import Any, Dict, List


from orca.driver_management.drivers.mock import MockTransporterDriver
from orca.resource_models.simulation_manager import SimulationManager
from orca.resource_models.resource_extras.teachpoints import Teachpoint
from orca.resource_models.transporter import Transporter

orca_logger = logging.getLogger("orca")

class HumanTransferDriver:
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

    async def initialize(self) -> None:
        orca_logger.info(f"Driver: {self._name} initialized successfully.")
        # Simulate initialization delay
        await asyncio.sleep(0.1)

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
    
class HumanTransfer(Transporter):
    """ A transportation method that requires human intervention to pick and place labware.
        It requests the user to manually pick up and place labware at specified teachpoints.  User must press Enter to continue after each action.
    """
    def __init__(self, name: str, teachpoints: str | List[str] | None = None, sim_time: float = 0.2, sim: bool = False) -> None:
        """ Initializes the HumanTransfer with a name, optional teachpoints, simulation time, and simulation mode.
        Args:
            name (str): The name of the HumanTransfer.
            teachpoints (str | List[str] | None): The teachpoints to use for the robotic arm, can be a file path or a list of position names.
            sim_time (float): The simulation time for the mock driver.
            sim (bool): Whether to run in simulation mode or not.
        """
        self._name = name
        self._sim_manager = SimulationManager(
            live_driver=HumanTransferDriver(name, teachpoints),
            sim_driver=MockTransporterDriver(name, "HumanTransfer", teachpoints, sim_time),
            sim=sim
        )
        self._lock = asyncio.Lock()
        super().__init__(name, self._sim_manager)
        self._is_initialized = False
   
    @property
    def is_initialized(self) -> bool:
        return self._is_initialized
    
    @property
    def name(self) -> str:
        return self._name
    
    @property
    def in_use(self) -> bool:
        return self._lock.locked()
    
    async def initialize(self) -> None:
        async with self._lock:
            await self._sim_manager.driver.initialize()
            self._is_initialized = True
            orca_logger.info(f"{self._name} initialized successfully.")

    async def _do_place(self, teachpoint_name: str, labware_type: str) -> None:
        return await self._sim_manager.driver.place(teachpoint_name, labware_type)

    async def _do_pick(self, teachpoint_name: str, labware_type: str) -> None:
        return await self._sim_manager.driver.pick(teachpoint_name, labware_type)

    def get_taught_positions(self) -> List[str]:
        return self._sim_manager.driver.get_taught_positions()