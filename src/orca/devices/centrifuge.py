from typing import ClassVar, Optional

from cheshire_drivers.centrifuge_models import CentrifugeRequest
from cheshire_drivers.interfaces import ICentrifugeDriver
from cheshire_drivers.sims import SimCentrifugeDriver

from orca.devices.device_interfaces import ICentrifuge
from orca.resource_models.devices import Device
from orca.runtime.device_factory_context import resolve_drivers
from orca.runtime.run_modes import WorkflowRunMode


class Centrifuge(Device[ICentrifugeDriver], ICentrifuge):
    KIND: ClassVar[str] = "centrifuge"

    def __init__(
        self,
        name: str,
        sim_override: WorkflowRunMode | None = None,
    ) -> None:
        live, sim_drv = resolve_drivers(
            self.KIND, name, SimCentrifugeDriver,
        )
        super().__init__(name, live, sim_drv, sim_override=sim_override)

    async def centrifuge(self, g: int, duration: int) -> None:
        await self.driver.centrifuge(CentrifugeRequest(g=float(g), duration=float(duration)))
