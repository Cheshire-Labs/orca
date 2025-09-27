import asyncio
import logging
from abc import ABC
from typing import Any, Dict, List, Optional, Protocol

from orca.resource_models.resource_extras.teachpoints import Teachpoint

orca_logger = logging.getLogger("orca")

class Sim(Protocol):
    def name(self) -> str: ...
    async def _sim(self, message: str) -> None: ...

class SimStrategy(ABC):
    async def sim(self, prompt: str) -> None:
        ...

class SleepSim(SimStrategy):
    def __init__(self, sim_time: float = 0.2) -> None:
        self.sim_time = sim_time

    async def sim(self, prompt: str) -> None:
        # keep the prompt visible in logs for traceability
        prompt = f"{prompt} (simulated wait for {self.sim_time} seconds)"
        orca_logger.info(prompt)
        await asyncio.sleep(self.sim_time)

class HumanSim(SimStrategy):
    """Waits for the user to press Enter without blocking the event loop."""
    def __init__(self, prompt_suffix: str = " Press Enter to continue.") -> None:
        self.prompt_suffix = prompt_suffix

    async def sim(self, prompt: str) -> None:
        full = f"{prompt}{self.prompt_suffix}"
        loop = asyncio.get_running_loop()
        # run blocking input() in a thread so we don't block the event loop
        # but this will block the current task/coroutine until input is received
        await loop.run_in_executor(None, lambda: input(full))


class BaseSimDriver:
    """Base simulation driver with common functionality"""
    
    def __init__(self, name: str, sim_strategy: Optional[SimStrategy] = None):
        self._name = name
        self._sim_strategy = sim_strategy or SleepSim()
        self._is_initialized: bool = False
        self._is_connected: bool = False
        self._is_running: bool = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def is_running(self) -> bool:
        return self._is_running

    async def initialize(self) -> None:
        await self._sim(f"Initialization of {self.name} in progress...")
        self._is_initialized = True
        orca_logger.info(f"{self.name} initialized successfully.")

    async def connect(self) -> None:
        await self._sim(f"Connecting {self.name}...")
        self._is_connected = True
        orca_logger.info(f"{self.name} connected successfully.")

    async def disconnect(self) -> None:
        await self._sim(f"Disconnecting {self.name}...")
        self._is_connected = False
        orca_logger.info(f"{self.name} disconnected successfully.")

    async def stop(self) -> None:
        """Stop the driver."""
        await self._sim(f"Stopping {self.name}...")
        self._is_running = False
        orca_logger.info(f"{self.name} stopped successfully.")

    async def _sim(self, message: str) -> None:
        self._is_running = True
        await self._sim(message)
        self._is_running = False
# Mixins for simulation functionality

class ShakerSimMixin(Sim):
    """Mixin for shaker simulation functionality"""
    _supports_locking = True

    async def shake(self, speed: float, duration: float) -> None:
        """Shake the device for a specified duration and speed."""
        self._is_running = True
        await self._sim(f"Shaking at speed {speed} for {duration} seconds...")
        orca_logger.info("Shaking completed successfully.")
        self._is_running = False

    async def stop_shaking(self) -> None:
        """Stop shaking"""
        await self._sim("Stopping shaking...")
        orca_logger.info("Shaking stopped.")

    @property
    def supports_locking(self) -> bool:
        """Check if the shaker supports locking the plate"""
        return self._supports_locking

    async def lock_plate(self) -> None:
        """Lock the plate"""
        await self._sim("Locking plate...")
        orca_logger.info("Plate locked.")

    async def unlock_plate(self) -> None:
        """Unlock the plate"""
        await self._sim("Unlocking plate...")
        orca_logger.info("Plate unlocked.")


class SealerSimMixin(Sim):
    """Mixin for sealer simulation functionality"""
    async def open(self) -> None:
        """Open the sealer."""
        self._is_running = True
        await self._sim("Opening sealer...")
        orca_logger.info("Sealer opened successfully.")
        self._is_running = False

    async def close(self) -> None:
        """Close the sealer."""
        self._is_running = True
        await self._sim("Closing sealer...")
        orca_logger.info("Sealer closed successfully.")
        self._is_running = False

    async def seal(self, temperature: int, duration: float) -> None:
        """Seal the plate at a specified temperature and duration."""
        self._is_running = True
        await self._sim(f"Sealing at {temperature}°C for {duration} seconds...")
        orca_logger.info("Sealing completed successfully.")
        self._is_running = False


class TempSettableSimMixin(Sim):
    """Mixin for temperature settable functionality"""
    
    async def set_temperature(self, temperature: float) -> None:
        """Set the temperature of the device."""
        await self._sim(f"Setting temperature to {temperature}°C...")
        orca_logger.info(f"Temperature set to {temperature}°C.")


class TempGettableSimMixin(Sim):
    """Mixin for temperature gettable functionality"""
    
    async def get_temperature(self) -> float:
        """Get the current temperature of the device."""
        orca_logger.info("Getting current temperature.")
        return 25.0  # Mock temperature value


class ProtocolRunnerSimMixin(Sim):
    """Mixin for protocol runner functionality"""
    
    async def run_protocol(self, protocol_filepath: str, params: Dict[str, Any]) -> None:
        """Run a protocol with given parameters."""
        self._is_running = True
        await self._sim(f"Running protocol: {protocol_filepath} with params: {params}...")
        orca_logger.info(f"Protocol {protocol_filepath} executed successfully.")
        self._is_running = False


class CentrifugeSimMixin(Sim):
    """Mixin for centrifuge functionality"""

    async def open(self) -> None:
        """Open the centrifuge."""
        self._is_running = True
        await self._sim("Opening centrifuge...")
        orca_logger.info("Centrifuge opened successfully.")
        self._is_running = False

    async def close(self) -> None:
        """Close the centrifuge."""
        self._is_running = True
        await self._sim("Closing centrifuge...")
        orca_logger.info("Centrifuge closed successfully.")
        self._is_running = False
    
    async def centrifuge(self, g: float, duration: float) -> None:
        """Spin the centrifuge at a specified speed for a specified duration."""
        self._is_running = True
        await self._sim(f"Spinning centrifuge for {duration} seconds at speed {g}...")
        orca_logger.info("Centrifuge spin completed successfully.")
        self._is_running = False


class ReaderSimMixin(Sim):
    """Mixin for reader functionality"""
    
    async def read(self, protocol_filepath: str, output_filepath: str) -> None:
        """Read data from the specified protocol and output to a file."""
        self._is_running = True
        await self._sim(f"Reading data from {protocol_filepath} and writing to {output_filepath}...")
        orca_logger.info(f"Data read successfully from {protocol_filepath} and written to {output_filepath}.")
        self._is_running = False


class DelidderSimMixin(Sim):
    """Mixin for delidder functionality"""
    
    async def delid(self) -> None:
        """Delid the specified labware."""
        self._is_running = True
        await self._sim("Delidding labware...")
        orca_logger.info("Labware delidded successfully.")
        self._is_running = False


class TransporterSimMixin(Sim):
    """Mixin for transporter functionality (robotic arms)"""
    def __init__(self):
        self._positions: Dict[str, Teachpoint]

    async def pick(self, position_name: str, labware_type: str) -> None:
        """Pick labware from a position."""
        self._validate_position(position_name)
        self._is_running = True
        await self._sim(f"Driver: {self.name} picking from {position_name}, labware type: {labware_type}...")
        orca_logger.info(f"Driver: {self.name} picked from {position_name}, labware type: {labware_type}")
        self._is_running = False

    async def place(self, position_name: str, labware_type: str) -> None:
        """Place labware at a position."""
        self._validate_position(position_name)
        self._is_running = True
        await self._sim(f"Driver: {self.name} placing to {position_name}, labware type: {labware_type}...")
        orca_logger.info(f"Driver: {self.name} placed to {position_name}, labware type: {labware_type}")
        self._is_running = False

    def _validate_position(self, position_name: str) -> None:
        """Validate that a position is taught."""
        if position_name not in self._positions:
            raise ValueError(f"The position '{position_name}' is not taught for {self.name}")

    def get_taught_positions(self) -> List[str]:
        """Get list of taught positions."""
        return list(self._positions.keys())

    def load_positions(self, positions: List[Teachpoint]) -> None:
        """Load taught positions."""
        self._positions = {t.name: t for t in positions}


class SimDriver(BaseSimDriver):
    """
    A simulation device driver that extends BaseSimDriver.
    """
    pass

class SimShakerDriver(BaseSimDriver, ShakerSimMixin):
    """Simulation shaker driver using mixin"""
    pass

class SimSealerDriver(BaseSimDriver, SealerSimMixin, TempSettableSimMixin, TempGettableSimMixin):
    """Simulation sealer driver using mixins"""
    pass

class SimCentrifugeDriver(BaseSimDriver, CentrifugeSimMixin):
    """Simulation centrifuge driver using mixin"""
    pass

class SimTransporterDriver(BaseSimDriver, TransporterSimMixin):
    """Simulation transporter driver using mixin"""
    pass
