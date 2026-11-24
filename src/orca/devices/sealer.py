from typing import ClassVar

from cheshire_drivers.interfaces import ISealerDriver
from cheshire_drivers.sealer_models import SealRequest

from orca.devices.device_interfaces import ISealer
from orca.resource_models.devices import Device
from orca.driver_management.device_factory_context import resolve_driver


class Sealer(Device[ISealerDriver], ISealer):
    KIND: ClassVar[str] = "sealer"

    def __init__(
        self,
        name: str,
    ) -> None:
        driver = resolve_driver(
            self.KIND, name, ISealerDriver,
        )
        super().__init__(name, driver)

    async def seal(self, temperature: int, duration: float) -> None:
        await self.driver.seal(SealRequest(temperature=temperature, duration=duration))
