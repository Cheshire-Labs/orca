from typing import ClassVar, List

from cheshire_drivers.interfaces import IThermocyclerDriver
from cheshire_drivers.sims import SimThermocyclerDriver
from cheshire_drivers.thermocycler_models import (
    CloseLidRequest,
    DeactivateBlockRequest,
    DeactivateLidRequest,
    GetBlockCurrentTemperatureRequest,
    GetBlockStatusRequest,
    GetBlockTargetTemperatureRequest,
    GetCurrentCycleIndexRequest,
    GetCurrentStepIndexRequest,
    GetHoldTimeRequest,
    GetLidCurrentTemperatureRequest,
    GetLidOpenRequest,
    GetLidStatusRequest,
    GetLidTargetTemperatureRequest,
    GetTotalCycleCountRequest,
    GetTotalStepCountRequest,
    OpenLidRequest,
    Protocol,
    RunProtocolRequest,
    SetBlockTemperatureRequest,
    SetLidTemperatureRequest,
)

from orca.devices.device_interfaces import IThermocycler
from orca.resource_models.devices import Device
from orca.runtime.device_factory_context import resolve_drivers
from orca.runtime.run_modes import WorkflowRunMode


class Thermocycler(Device[IThermocyclerDriver], IThermocycler):
    """Driver for a thermocycler device."""

    KIND: ClassVar[str] = "thermocycler"

    def __init__(
        self,
        name: str,
        sim_override: WorkflowRunMode | None = None,
    ) -> None:
        live, sim_drv = resolve_drivers(
            self.KIND, name, SimThermocyclerDriver,
        )
        super().__init__(name, live, sim_drv, sim_override=sim_override)

    async def run_protocol(self, protocol: Protocol, block_max_volume: float) -> None:
        await self.driver.run_protocol(
            RunProtocolRequest(protocol=protocol, block_max_volume=block_max_volume)
        )

    async def open_lid(self) -> None:
        await self.driver.open_lid(OpenLidRequest())

    async def close_lid(self) -> None:
        await self.driver.close_lid(CloseLidRequest())

    async def set_block_temperature(self, temperature: List[float]) -> None:
        await self.driver.set_block_temperature(
            SetBlockTemperatureRequest(temperature=temperature)
        )

    async def set_lid_temperature(self, temperature: List[float]) -> None:
        await self.driver.set_lid_temperature(
            SetLidTemperatureRequest(temperature=temperature)
        )

    async def deactivate_block(self) -> None:
        await self.driver.deactivate_block(DeactivateBlockRequest())

    async def deactivate_lid(self) -> None:
        await self.driver.deactivate_lid(DeactivateLidRequest())

    async def get_block_current_temperature(self) -> List[float]:
        response = await self.driver.get_block_current_temperature(
            GetBlockCurrentTemperatureRequest()
        )
        return response.temperatures

    async def get_block_target_temperature(self) -> List[float]:
        response = await self.driver.get_block_target_temperature(
            GetBlockTargetTemperatureRequest()
        )
        return response.temperatures

    async def get_lid_current_temperature(self) -> List[float]:
        response = await self.driver.get_lid_current_temperature(
            GetLidCurrentTemperatureRequest()
        )
        return response.temperatures

    async def get_lid_target_temperature(self) -> List[float]:
        response = await self.driver.get_lid_target_temperature(
            GetLidTargetTemperatureRequest()
        )
        return response.temperatures

    async def get_lid_open(self) -> bool:
        response = await self.driver.get_lid_open(GetLidOpenRequest())
        return response.open

    async def get_lid_status(self) -> str:
        response = await self.driver.get_lid_status(GetLidStatusRequest())
        return response.status

    async def get_block_status(self) -> str:
        response = await self.driver.get_block_status(GetBlockStatusRequest())
        return response.status

    async def get_hold_time(self) -> float:
        response = await self.driver.get_hold_time(GetHoldTimeRequest())
        return response.seconds

    async def get_current_cycle_index(self) -> int:
        response = await self.driver.get_current_cycle_index(
            GetCurrentCycleIndexRequest()
        )
        return response.index

    async def get_total_cycle_count(self) -> int:
        response = await self.driver.get_total_cycle_count(GetTotalCycleCountRequest())
        return response.count

    async def get_current_step_index(self) -> int:
        response = await self.driver.get_current_step_index(
            GetCurrentStepIndexRequest()
        )
        return response.index

    async def get_total_step_count(self) -> int:
        response = await self.driver.get_total_step_count(GetTotalStepCountRequest())
        return response.count
