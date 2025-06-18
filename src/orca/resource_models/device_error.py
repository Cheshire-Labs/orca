from typing import Optional


class DeviceError(Exception):
    """Base class for all device-related errors."""
    def __init__(self, message: str, device_name: Optional[str] = None) -> None:
        self.device_name = device_name
        self.message = message if device_name is None else f"[{device_name}] {message}"
        super().__init__(self.message)


class DeviceBusyError(DeviceError):
    """Raised when a device is already in use or staged."""
    pass


class DeviceNotInitializedError(DeviceError):
    """Raised when an operation is attempted on a device that hasn't been initialized."""
    pass