from abc import ABC, abstractmethod
from typing import Any, Dict

class IGenericExecutable(ABC):
    @abstractmethod
    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        """Execute a command with the driver."""
        raise NotImplementedError("Subclasses must implement this method.")
    
class ITempSettable(ABC):
    @abstractmethod
    async def set_temperature(self, temperature: float) -> None:
        """Set the temperature of the device."""
        raise NotImplementedError("Subclasses must implement this method.")


class ITempGettable(ABC):
    @abstractmethod
    async def get_temperature(self) -> float:
        """Get the current temperature of the device."""
        raise NotImplementedError("Subclasses must implement this method.")

class ISealer(ABC):
    @abstractmethod
    async def seal(self, temperature: int, duration: float) -> None:
        """Seal the plate at a specified temperature and duration."""
        raise NotImplementedError("Subclasses must implement this method.")

class IProtocolRunner(ABC):
    @abstractmethod
    async def run_protocol(self, protocol_filepath: str, params: Dict[str, Any]) -> None:
        """Execute a protocol run command."""
        raise NotImplementedError("Subclasses must implement this method.")
    
class IShaker(ABC):
    @abstractmethod
    async def shake(self, duration: int, speed: int) -> None:
        """Shake the device for a specified duration and speed."""
        raise NotImplementedError("Subclasses must implement this method.")

class ICentrifuge(ABC):
    @abstractmethod
    async def centrifuge(self, speed: int, duration: int) -> None:
        """Spin the centrifuge at a specified speed for a specified duration."""
        raise NotImplementedError("Subclasses must implement this method.")
    
class IReader(ABC):
    @abstractmethod
    async def read(self, protocol_filepath: str, output_filepath: str) -> None:
        """Read data using the specified protocol and save results to a file."""
        raise NotImplementedError("Subclasses must implement this method.")
    
class IDelidder(ABC):
    @abstractmethod
    async def delid(self) -> None:
        """Remove the lid from the specified labware."""
        raise NotImplementedError("Subclasses must implement this method.")