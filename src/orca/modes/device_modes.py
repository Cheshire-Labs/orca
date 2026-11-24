"""Whether each device runs as sim or live, kept in the activity database."""

from enum import Enum
from typing import Protocol


class DeviceMode(str, Enum):
    SIM = "SIM"
    LIVE = "LIVE"


class IDeviceModes(Protocol):
    """Read from the database on every call; nothing keeps a copy."""

    async def mode_of(self, device_name: str) -> DeviceMode:
        """SIM for a device with no setting, so nothing moves real hardware until someone sets it to live."""
        ...

    async def set(self, device_name: str, mode: DeviceMode) -> None: ...
