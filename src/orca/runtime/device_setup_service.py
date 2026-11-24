"""DeviceSetupService: one lock over the `devices/<name>.json` files, so two writers never interleave."""

import asyncio
from typing import Protocol

from cheshire_drivers.device_setup import DeviceSetup

from orca.resource_models.store_interfaces import IDeviceSetupStore


class IWritableDeviceSetupStore(IDeviceSetupStore, Protocol):
    def write(self, name: str, setup: DeviceSetup) -> None:
        """Replace the device's setup whole, atomically."""
        ...


class IDeviceSetupService(IDeviceSetupStore, Protocol):
    async def put(self, name: str, setup: DeviceSetup) -> None: ...


class DeviceSetupService:
    def __init__(self, store: IWritableDeviceSetupStore) -> None:
        self._store = store
        self._lock = asyncio.Lock()

    @property
    def default_client(self) -> str:
        return self._store.default_client

    def names(self) -> list[str]:
        return self._store.names()

    def read(self, name: str) -> DeviceSetup:
        return self._store.read(name)

    async def put(self, name: str, setup: DeviceSetup) -> None:
        async with self._lock:
            await asyncio.to_thread(self._store.write, name, setup)
