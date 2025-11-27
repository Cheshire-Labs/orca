from abc import ABC, abstractmethod
from typing import Any, Dict, List


from orca.resource_models.resource_extras.teachpoints import Teachpoint


class BaseDriver(ABC):
    @abstractmethod
    async def initialize(self) -> None:
        """Setup the driver."""
        ...
    
    @property
    @abstractmethod
    def is_initialized(self) -> bool:
        """Returns whether the driver is initialized or not."""
        ...

    @abstractmethod
    async def open(self) -> None:
        """Open driver door."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close driver door."""
        ...



class IShakerDriver(BaseDriver, ABC):

    @abstractmethod
    async def stop(self) -> None:
        """Stop the shaker backend."""
        ...

    @property
    @abstractmethod
    def supports_locking(self) -> bool:
        """Check if the shaker supports locking the plate"""
        ...

    @abstractmethod
    async def unlock_plate(self) -> None:
        """Unlock the plate"""
        ...

    @abstractmethod
    async def lock_plate(self):
        """Lock the plate"""
        ...

    @abstractmethod
    async def shake(self, speed: float, duration: float) -> None:
        """Shake the shaker at the given speed

        Args:
            speed: Speed of shaking in revolutions per minute (RPM)
            duration: Duration of shaking in seconds
        """
        ...

    @abstractmethod
    async def stop_shaking(self) -> None:
        """Stop shaking"""
        ...


class ISealerDriver(BaseDriver, ABC):

    @abstractmethod
    async def seal(self, temperature: int, duration: float) -> None:
        """Seal at specified temperature and duration."""
        ...

    @abstractmethod
    async def set_temperature(self, temperature: float) -> None:
        """Set the temperature."""
        ...

    @abstractmethod
    async def get_temperature(self) -> float:
        """Get the current temperature."""
        ...



class ITempSettableDriver(ABC):
    
    @abstractmethod
    async def set_temperature(self, temperature: float) -> None:
        """Set the temperature of the device."""
        ...


class ITempGettableDriver(ABC):
    @abstractmethod
    async def get_temperature(self) -> float:
        """Get the current temperature of the device."""
        ...


class IProtocolRunnerDriver(BaseDriver, ABC):
    @abstractmethod
    async def run_protocol(self, protocol_filepath: str, params: Dict[str, Any]) -> None:
        """Execute a protocol run command."""
        ...    
   

class ICentrifugeDriver(BaseDriver, ABC):
    @abstractmethod
    async def centrifuge(self, g: float, duration: float) -> None:
        """Spin the centrifuge at a specified speed for a specified duration."""
        ...  


class IReaderDriver(BaseDriver, ABC):
    @abstractmethod
    async def read(self, protocol_filepath: str, output_filepath: str) -> None:
        """Read data using the specified protocol and save results to a file."""
        ...


class IDelidderDriver(BaseDriver, ABC):
    @abstractmethod
    async def delid(self) -> None:
        """Remove the lid from the specified labware."""
        ...


class ITransporterDriver(ABC):

    @property
    @abstractmethod
    def name(self) -> str:
        """Returns the name of the transporter."""
        ...

    @abstractmethod
    async def initialize(self) -> None:
        """Setup the driver."""
        ...

    @abstractmethod
    async def home(self) -> None:
        """Homes the transporter."""
        ...

    @abstractmethod
    async def move_to_safe(self) -> None:
        """Moves the transporter to a safe position."""
        ...
    
    @property
    @abstractmethod
    def is_initialized(self) -> bool:
        """Returns whether the driver is initialized or not."""
        ...

    @abstractmethod
    async def pick(self, position_name: str, labware_type: str) -> None:
        ...

    @abstractmethod
    async def place(self, position_name: str, labware_type: str) -> None:
        ...

    @abstractmethod
    def get_teachpoints(self) -> List[Teachpoint]:
        ...

    @abstractmethod
    def load_teachpoints(self, teachpoints: List[Teachpoint]) -> None:
        ...

    @abstractmethod
    async def move_to_position(self, position_name: str) -> None:
        """Move to a position without picking/placing (for waypoint traversal)."""
        ...


class IStorageDriver(BaseDriver, ABC):
    pass


class IPlateWasherDriver(IProtocolRunnerDriver, ABC):
    pass


class ILiquidHandlerDriver(IProtocolRunnerDriver, ABC):
    pass


class IWasteDriver(IStorageDriver, ABC):
    pass