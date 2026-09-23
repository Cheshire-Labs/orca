from typing import ClassVar, Optional

from cheshire_drivers.interfaces import IShakerDriver
from cheshire_drivers.shaker_models import ShakeRequest
from cheshire_drivers.sims import SimShakerDriver

from orca.devices.device_interfaces import IShaker
from orca.resource_models.devices import Device
from orca.runtime.device_factory_context import resolve_drivers
from orca.runtime.run_modes import WorkflowRunMode


class Shaker(Device[IShakerDriver], IShaker):
    """Driver for a shaker device."""

    KIND: ClassVar[str] = "shaker"

    def __init__(
        self,
        name: str,
        sim_override: WorkflowRunMode | None = None,
    ) -> None:
        live, sim_drv = resolve_drivers(
            self.KIND, name, SimShakerDriver,
        )
        super().__init__(name, live, sim_drv, sim_override=sim_override)

    async def shake(self, duration: int, speed: int) -> None:
        await self.driver.shake(ShakeRequest(speed=float(speed), duration=float(duration)))
