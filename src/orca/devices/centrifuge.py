from typing import ClassVar

from cheshire_drivers.centrifuge_models import CentrifugeRequest
from cheshire_drivers.interfaces import ICentrifugeDriver

from orca.devices.device_interfaces import ICentrifuge
from orca.resource_models.devices import Device
from orca.driver_management.device_factory_context import resolve_driver


class Centrifuge(Device[ICentrifugeDriver], ICentrifuge):
    KIND: ClassVar[str] = "centrifuge"

    def __init__(
        self,
        name: str,
    ) -> None:
        driver = resolve_driver(
            self.KIND, name, ICentrifugeDriver,
        )
        super().__init__(name, driver)

    async def centrifuge(self, g: int, duration: int) -> None:
        await self.driver.centrifuge(CentrifugeRequest(g=float(g), duration=float(duration)))
