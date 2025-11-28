import asyncio
import logging
from typing import List, Optional, Any
from cheshire_drivers import PLRTransporterBackendWrapper, ITransporterDriver, SimTransporterDriver, Teachpoint
from orca.resource_models.simulation_manager import SimulationManager
from orca.resource_models.transporter_interface import ITransporter
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location


from pylabrobot.arms.backend import ArmBackend


orca_logger = logging.getLogger("orca")


class Transporter(ITransporter):
    def __init__(self,
                name: str,
                driver: ITransporterDriver | ArmBackend,
                load_positions: Optional[List[Teachpoint] | str] = None,
                sim: bool = False) -> None:
        self._name = name
        driver = PLRTransporterBackendWrapper(driver) if isinstance(driver, ArmBackend) else driver
        self._sim_manager = SimulationManager(
            driver,
            SimTransporterDriver("sim"),
            sim
            )
        self._lock = asyncio.Lock()
        self._labware: Optional[LabwareInstance] = None
        if type(load_positions) is str:
            teachpoints = Teachpoint.load_teachpoints_from_file(load_positions)
            self.load_teachpoints(teachpoints)
        elif type(load_positions) is list:
            self.load_teachpoints(load_positions)
        else:
            self.load_teachpoints([])

    @property
    def name(self) -> str:
        """Returns the name of the transporter."""
        return self._name
    
    @property
    def driver(self) -> ITransporterDriver:
        return self._sim_manager.driver

    @property
    def is_initialized(self) -> bool:
        """Returns whether the transporter is initialized or not."""
        return self.driver.is_initialized
    
    async def initialize(self) -> None:
        """Initializes the transporter."""
        await self.driver.initialize()
        await self.driver.home()
        await self.driver.move_to_safe()

    @property
    def lock(self) -> asyncio.Lock:
        """Returns the lock for the transporter."""
        return self._lock

    @property
    def in_use(self) -> bool:
        """Returns whether the transporter is running or not."""
        return self._lock.locked()

    @property
    def is_simulating(self) -> bool:
        """Returns whether the transporter is simulating or not."""
        return self._sim_manager.is_simulating
    
    def set_simulating(self, sim: bool) -> None:
        """Sets the simulation state of the transporter."""
        self._sim_manager.set_simulating(sim)

    @property
    def labware(self) -> Optional[LabwareInstance]:
        return self._labware
    
    async def pick(self, location: Location) -> None:
        if self._labware is not None:
            raise ValueError(f"{self} already contains labware: {self._labware}")
        if location.labware is None:
            raise ValueError(f"{location} does not contain labware")
        orca_logger.info(f"{self._name} pick {location.labware} from {location}: picking...")
        await self.driver.pick(location.teachpoint_name, location.labware.labware_type)
        orca_logger.info(f"{self._name} pick {location.labware} from {location}: picked")
        self._labware = location.labware

    async def place(self, location: Location) -> None:
        if self._labware is None:
            raise ValueError(f"{self} does not contain labware")
        if location.labware is not None:
            raise ValueError(f"{location} already contains labware")
        orca_logger.info(f"{self._name} place {self._labware} to {location}: placing...")
        await self.driver.place(location.teachpoint_name, self._labware.labware_type)
        orca_logger.info(f"{self._name} place {self._labware} to {location}: placed")
        
        self._labware = None

    def get_teachpoints(self) -> List[Teachpoint]:
        return self.driver.get_teachpoints()
    
    def load_teachpoints(self, teachpoints: List[Teachpoint]) -> None:
        self.driver.load_teachpoints(teachpoints)

    def pull_teachpoints_from_robot(self) -> List[Teachpoint]:
        return self.driver.get_teachpoints()
        
    def __str__(self) -> str:
        return f"Transporter: {self._name}"