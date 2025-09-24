from typing import List
from orca.driver_management.drivers.mock import MockTransporter
from orca.resource_models.simulation_manager import SimulationManager
from orca.resource_models.transporter import Transporter
from pylabrobot.arms.backend import ArmBackend as PLRARmBackend


class RoboticArm(Transporter):
    def __init__(self, name: str, backend: PLRARmBackend, sim: bool = False) -> None:
        self._name = name
        self._sim_manager = SimulationManager(
            backend,
            MockTransporter("sim", sim_time=0.1),
            sim
            )
        super().__init__(name, self._sim_manager)

    @property
    def driver(self) -> PLRARmBackend | MockTransporter:
        return self._sim_manager.driver
    
    @property
    def name(self) -> str:
        return self._name
    
    @property
    def is_initialized(self) -> bool:
        return self._is_initialized
    
    async def initialize(self) -> None:
        """
        Initialize the driver.
        """
        await self.driver.setup(**self._init_options)  # type: ignore
        self._is_initialized = True
    


    #### HERE.   FILL THIS OUT ####

    async def _do_pick(self, teachpoint_name: str, labware_type: str) -> None:
        raise NotImplementedError("This method should be implemented by subclasses.")
    
    async def _do_place(self, teachpoint_name: str, labware_type: str) -> None:
        raise NotImplementedError("This method should be implemented by subclasses.")

    def get_taught_positions(self) -> List[str]:
        raise NotImplementedError("This method should be implemented by subclasses.")