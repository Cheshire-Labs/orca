from typing import ClassVar, Dict, Optional

from cheshire_drivers.interfaces import ISealerDriver
from cheshire_drivers.sealer_models import SealRequest
from cheshire_drivers.sims import SimSealerDriver

from orca.devices.device_interfaces import ISealer
from orca.resource_models.devices import Device
from orca.runtime.device_factory_context import resolve_drivers
from orca.runtime.run_modes import WorkflowRunMode


class Sealer(Device[ISealerDriver], ISealer):
    KIND: ClassVar[str] = "sealer"

    def __init__(
        self,
        name: str,
        sim_override: WorkflowRunMode | None = None,
    ) -> None:
        live, sim_drv = resolve_drivers(
            self.KIND, name, SimSealerDriver,
        )
        super().__init__(name, live, sim_drv, sim_override=sim_override)

    async def execute(self, command: str, options: Dict[str, str]) -> None:
        if command == "seal":
            temperature = int(options.get("temperature", 0))
            duration = float(options.get("duration", 0.0))
            await self.seal(temperature=temperature, duration=duration)
        elif command == "open":
            await self.driver.open()
        elif command == "close":
            await self.driver.close()
        else:
            raise ValueError(f"Unknown command: {command}")

    async def seal(self, temperature: int, duration: float) -> None:
        await self.driver.seal(SealRequest(temperature=temperature, duration=duration))
