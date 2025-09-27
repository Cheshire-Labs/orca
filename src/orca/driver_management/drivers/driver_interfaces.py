from abc import ABC, abstractmethod
from typing import Any, Dict, List
from typing_extensions import Protocol, runtime_checkable

from orca.resource_models.resource_extras.teachpoints import Teachpoint





@runtime_checkable
class BaseDriver(Protocol):
    async def initialize(self) -> None:
        """Setup the driver."""
        ...
    
    @property
    def is_initialized(self) -> bool:
        """Returns whether the driver is initialized or not."""
        ...

    async def open(self) -> None:
        """Open driver door."""
        ...

    async def close(self) -> None:
        """Close driver door."""
        ...


@runtime_checkable
class IShakerDriver(BaseDriver, Protocol):

    async def stop(self) -> None:
        """Stop the shaker backend."""
        ...

    @property
    def supports_locking(self) -> bool:
        """Check if the shaker supports locking the plate"""
        ...

    async def unlock_plate(self) -> None:
        """Unlock the plate"""
        ...

    async def lock_plate(self):
        """Lock the plate"""
        ...

    async def shake(self, speed: float, duration: float) -> None:
        """Shake the shaker at the given speed

        Args:
            speed: Speed of shaking in revolutions per minute (RPM)
            duration: Duration of shaking in seconds
        """
        ...

    async def stop_shaking(self) -> None:
        """Stop shaking"""
        ...

@runtime_checkable
class ISealerDriver(BaseDriver, Protocol):

    async def seal(self, temperature: int, duration: float) -> None:
        """Seal at specified temperature and duration."""
        ...

    async def set_temperature(self, temperature: float) -> None:
        """Set the temperature."""
        ...

    async def get_temperature(self) -> float:
        """Get the current temperature."""
        ...


@runtime_checkable
class ITempSettableDriver(Protocol):
    async def set_temperature(self, temperature: float) -> None:
        """Set the temperature of the device."""
        ...

@runtime_checkable
class ITempGettableDriver(Protocol):
    async def get_temperature(self) -> float:
        """Get the current temperature of the device."""
        ...

@runtime_checkable
class IProtocolRunnerDriver(BaseDriver, Protocol):
    async def run_protocol(self, protocol_filepath: str, params: Dict[str, Any]) -> None:
        """Execute a protocol run command."""
        ...    
   
@runtime_checkable
class ICentrifugeDriver(BaseDriver, Protocol):

    async def centrifuge(self, g: float, duration: float) -> None:
        """Spin the centrifuge at a specified speed for a specified duration."""
        ...  

@runtime_checkable
class IReaderDriver(BaseDriver, Protocol):
    async def read(self, protocol_filepath: str, output_filepath: str) -> None:
        """Read data using the specified protocol and save results to a file."""
        ...

@runtime_checkable
class IDelidderDriver(BaseDriver, Protocol):
    async def delid(self) -> None:
        """Remove the lid from the specified labware."""
        ...

@runtime_checkable
class ITransporterDriver(BaseDriver, Protocol):

    @property
    def name(self) -> str:
        """Returns the name of the transporter."""
        ...

    async def pick(self, position_name: str, labware_type: str) -> None:
        ...

    async def place(self, position_name: str, labware_type: str) -> None:
        ...

    def get_taught_positions(self) -> List[str]:
        ...

    def load_positions(self, positions: List[Teachpoint]) -> None:
        ...

@runtime_checkable
class IStorageDriver(BaseDriver, Protocol):
    pass

@runtime_checkable
class IPlateWasherDriver(IProtocolRunnerDriver, Protocol):
    pass

@runtime_checkable
class ILiquidHandlerDriver(IProtocolRunnerDriver, Protocol):
    pass

@runtime_checkable
class IWasteDriver(IStorageDriver, Protocol):
    pass