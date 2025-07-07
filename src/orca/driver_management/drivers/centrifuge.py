import asyncio
from orca.driver_management.driver_interfaces import ICentrifuge

from orca.resource_models.devices import Device
from pylabrobot.centrifuge.backend import CentrifugeBackend

class CentrifugeDriver(Device, ICentrifuge):
    def __init__(self, name: str, backend: CentrifugeBackend):
        self._name = name
        self._centrifuge = backend
        self._is_initialized: bool = False
        self._lock = asyncio.Lock()