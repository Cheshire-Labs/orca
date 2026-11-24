"""Each device's setup file, `devices/<name>.json`: how it runs. A change applies on the next reload."""

from collections.abc import Iterable
from abc import ABC, abstractmethod

from cheshire_drivers.device_setup import DeviceSetup

from orca.resource_models.devices import Device
from orca.resource_models.site_rack import SiteRack
from orca.resource_models.transporter import Transporter
from orca.runtime.danger import DangerLevel, dangerous
from orca.runtime.device_assignment import check_setup_fits
from orca.runtime.device_setup_service import IDeviceSetupService


class IDeviceSetupFacade(ABC):
    @abstractmethod
    def read(self, device_name: str) -> DeviceSetup:
        """The setup as the file holds it; a device with no file reads as the default sim."""
        ...

    @abstractmethod
    async def put(
        self, device_name: str, setup: DeviceSetup,
        resources: Iterable[Device | Transporter | SiteRack] | None = None,
        *, reason: str | None = None, confirm: bool = False,
    ) -> None:
        """Write the file. With the loaded system's resources, refuse a setup the next load would refuse."""
        ...


class DeviceSetupFacade(IDeviceSetupFacade):
    def __init__(self, service: IDeviceSetupService) -> None:
        self._service = service

    def read(self, device_name: str) -> DeviceSetup:
        return self._service.read(device_name)

    @dangerous(
        name="device_setups.put",
        level=DangerLevel.OPERATOR,
        message="Write devices/{device_name}.json: which orca-client runs '{device_name}', its driver and "
                "connection, and its sim settings. Takes effect on the next reload.",
    )
    async def put(
        self, device_name: str, setup: DeviceSetup,
        resources: Iterable[Device | Transporter | SiteRack] | None = None,
        *, reason: str | None = None,
    ) -> None:
        del reason  # consumed by @dangerous audit
        if resources is not None:
            check_setup_fits(resources, device_name, setup, self._service.default_client)
        await self._service.put(device_name, setup)
