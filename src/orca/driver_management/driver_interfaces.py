from abc import ABC, abstractmethod
from typing import Any, Dict


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


class IShaker(ABC):
    @abstractmethod
    async def shake(self, temperature: float, duration: int) -> None:
        """Execute a shake command."""
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