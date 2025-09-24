from orca.driver_management.driver_interfaces import ISealer
from orca.driver_management.drivers.sealer import ISealerBackend, PLRSealerBackendWrapper, SimulationSealerBackend
from orca.resource_models.devices import Device
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.simulation_manager import SimulationManager
from pylabrobot.sealing.backend import SealerBackend as PLRSealerBackend


from typing import Dict


class Sealer(Device, ISealer):
    def __init__(self, name: str, backend: PLRSealerBackend, sim: bool = False) -> None:
        self._name = name
        self._sim_manager = SimulationManager(
            PLRSealerBackendWrapper(backend),
            SimulationSealerBackend("sim", sim_time=0.1),
            sim
            )
        super().__init__(name, self._sim_manager)

    @property
    def driver(self) -> ISealerBackend:
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

    async def execute(self, command: str, options: Dict[str, str]) -> None:
        if command == "seal":
            temperature = int(options.get("temperature", 0))
            duration = float(options.get("duration", 0.0))
            await self.seal(temperature=temperature, duration=duration)
        elif command == "open":
            await self.driver.open()
        elif command == "close":
            await self.driver.close()
        else:
            raise ValueError(f"Unknown command: {command}")

    async def _do_prepare_for_pick(self, labware: LabwareInstance) -> None:
        await self.driver.open()

    async def _do_prepare_for_place(self, labware: LabwareInstance) -> None:
        await self.driver.open()

    async def _do_notify_picked(self, labware: LabwareInstance) -> None:
        await self.driver.close()

    async def _do_notify_placed(self, labware: LabwareInstance) -> None:
        await self.driver.close()

    async def seal(self, temperature: int, duration: float) -> None:
        """Seal the plate at a specified temperature and duration."""
        await self.driver.seal(temperature=temperature, duration=duration)