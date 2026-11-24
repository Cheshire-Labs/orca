from typing import ClassVar

from cheshire_drivers.interfaces import IShakerDriver
from cheshire_drivers.shaker_models import ShakeRequest

from orca.devices.device_interfaces import IShaker
from orca.resource_models.devices import Device
from orca.driver_management.device_factory_context import resolve_driver


class Shaker(Device[IShakerDriver], IShaker):
    """Driver for a shaker device."""

    KIND: ClassVar[str] = "shaker"

    def __init__(
        self,
        name: str,
    ) -> None:
        driver = resolve_driver(
            self.KIND, name, IShakerDriver,
        )
        super().__init__(name, driver)

    async def shake(self, duration: int, speed: int) -> None:
        await self.driver.shake(ShakeRequest(speed=float(speed), duration=float(duration)))
