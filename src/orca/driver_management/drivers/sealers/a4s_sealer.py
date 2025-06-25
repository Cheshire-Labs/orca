import logging
import typing
import asyncio
from typing import Any, Dict


from orca_driver_interface.driver_interfaces import ILabwarePlaceableDriver

from orca.driver_management.driver_interfaces import ITempSettable
from orca.driver_management.driver_interfaces import ITempGettable
from orca.driver_management.driver_interfaces import ISealer

if typing.TYPE_CHECKING:
    from pylabrobot.sealing.a4s_backend import A4SBackend as PLRA4SBackend

orca_logger = logging.getLogger("orca")
class A4SSealerDriver(ILabwarePlaceableDriver, ISealer, ITempSettable, ITempGettable):
    def __init__(self, port: str, timeout: int):
        try:
            from pylabrobot.sealing.a4s_backend import A4SBackend as PLRA4SBackend
        except ImportError:
            raise ImportError("pylabrobot is not installed. Please install it to use A4SSealerDriver.")
        
        self.port = port
        self.timeout = timeout
        self._sealer: PLRA4SBackend | None = None
        self._is_initialized: bool = False
        self._lock: asyncio.Lock = asyncio.Lock()


    @property
    def sealer(self) -> "PLRA4SBackend":
        if self._sealer is None:
            raise ValueError("The driver must be connected first.")
        return self._sealer
    
    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        """
        Set the initialization options for the driver.
        Args:
            init_options (Dict[str, Any]): The initialization options.
        """
        self._init_options = init_options

    async def initialize(self) -> None:
        """
        Initialize the driver.
        """
        async with self._lock:
            await self.sealer.setup(**self._init_options)  # type: ignore

    @property
    def is_connected(self) -> bool:
        """
        Check if the driver is connected.
        Returns:
            bool: True if the driver is connected, False otherwise.
        """
        return not self.sealer.io.closed

    async def connect(self) -> None:
        """
        Connect to the driver.
        """
        if self.is_connected:
            orca_logger.warning("Already connected to the sealer.")
            return
        async with self._lock:
            self._sealer = PLRA4SBackend(self.port, self.timeout)
            await self.sealer.io.setup()

    async def disconnect(self) -> None:
        """
        Disconnect from the driver.
        """
        await self.sealer.stop()
        self._is_initialized = False
        self.sealer.io.close()

    @property
    def name(self) -> str:
        return "A4S Sealer"

    @property
    def is_running(self) -> bool:
        """Check if the driver is running."""
        return self._lock.locked()

    async def execute(self, command: str, options: Dict[str, str]) -> None:
        if command == "seal":
            temperature = int(options.get("temperature", 0))
            duration = float(options.get("duration", 0.0))
            await self.seal(temperature=temperature, duration=duration)
        elif command == "open":
            await self.sealer.open()
        elif command == "close":
            await self.sealer.close()
        elif command == "set_temperature":
            set_temp = float(options.get("temperature", 0.0))
            await self.set_temperature(set_temp)
        elif command == "get_temperature":
            temp = await self.get_temperature()
            orca_logger.info(f"Current temperature: {temp}")
        else:
            raise ValueError(f"Unknown command: {command}")

    async def prepare_for_pick(self, labware_name: str, labware_type: str, barcode: str | None = None, alias: str | None = None) -> None:
        async with self._lock:
            await self.sealer.open()

    async def prepare_for_place(self, labware_name: str, labware_type: str, barcode: str | None = None, alias: str | None = None) -> None:
        async with self._lock:
            await self.sealer.open()

    async def notify_picked(self, labware_name: str, labware_type: str, barcode: str | None = None, alias: str | None = None) -> None:
        async with self._lock:
            await self.sealer.close()

    async def notify_placed(self, labware_name: str, labware_type: str, barcode: str | None = None, alias: str | None = None) -> None:
        async with self._lock:
            await self.sealer.close()

    async def seal(self, temperature: int, duration: float) -> None:
        """Seal the plate at a specified temperature and duration."""
        async with self._lock:
            await self.sealer.seal(temperature=temperature, duration=duration)

    async def set_temperature(self, temperature: float) -> None:
        """Set the temperature of the sealer."""
        async with self._lock:
            await self.sealer.set_temperature(temperature=temperature)

    async def get_temperature(self) -> float:
        """Get the current temperature of the sealer."""
        async with self._lock:
            return await self.sealer.get_temperature()



    

    