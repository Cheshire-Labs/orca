from typing import Any, Dict, runtime_checkable
from typing_extensions import Protocol

@runtime_checkable
class IGenericExecutable(Protocol):
    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        """Execute a command with the driver."""
        ...

@runtime_checkable
class ITempSettable(Protocol):
    async def set_temperature(self, temperature: float) -> None:
        """Set the temperature of the device."""
        ...

@runtime_checkable
class ITempGettable(Protocol):
    async def get_temperature(self) -> float:
        """Get the current temperature of the device."""
        ...

@runtime_checkable
class ISealer(Protocol):
    async def seal(self, temperature: int, duration: float) -> None:
        """Seal the plate at a specified temperature and duration."""
        ...

@runtime_checkable
class IProtocolRunner(Protocol):
    async def run_protocol(self, protocol_filepath: str, params: Dict[str, Any]) -> None:
        """Execute a protocol run command."""
        ...    
    
@runtime_checkable
class IShaker(Protocol):
    async def shake(self, duration: int, speed: int) -> None:
        """Shake the device for a specified duration and speed."""
        ...

@runtime_checkable
class ICentrifuge(Protocol):
    async def centrifuge(self, speed: int, duration: int) -> None:
        """Spin the centrifuge at a specified speed for a specified duration."""
        ...  

@runtime_checkable  
class IReader(Protocol):
    async def read(self, protocol_filepath: str, output_filepath: str) -> None:
        """Read data using the specified protocol and save results to a file."""
        ...

@runtime_checkable
class IDelidder(Protocol):
    async def delid(self) -> None:
        """Remove the lid from the specified labware."""
        ...