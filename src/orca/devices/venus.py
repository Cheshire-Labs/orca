from contextlib import nullcontext

from cheshire_drivers.protocol_runner_models import LabwareHandoffRequest
from cheshire_drivers.venus_driver import SimulationVenusProtocolDriver
from orca.devices.devices import LiquidHandlerProtocol
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placeable_interface import IPlateMover
from orca.runtime.device_factory_context import is_factory_bound, use_device_factory
from orca.runtime.device_factory_protocol import DriverPairElement
from orca.runtime.run_modes import WorkflowRunMode


class Venus(LiquidHandlerProtocol):
    """A Hamilton liquid handler run by Venus methods.

    Each pick or place on one of its sites runs the matching Venus hook method
    with the labware and the site. HxRun.exe, the methods folder and the hook
    methods are set in orca-client's venus settings.
    """

    def __init__(
        self,
        name: str,
        *,
        sim_override: WorkflowRunMode | None = None,
        site_names: list[str] | None = None,
    ) -> None:
        # With no factory bound (PURE_SIM), simulate Venus methods rather than a generic handler.
        sim_only = nullcontext() if is_factory_bound() else use_device_factory(_VenusSimulator())
        with sim_only:
            super().__init__(name, sim_override=sim_override, site_names=site_names)

    async def _do_prepare_for_place(self, labware: LabwareInstance, mover: IPlateMover, target: str | None = None, site: str | None = None) -> None:
        await self.driver.prepare_for_place(_handoff(labware, site))

    async def _do_notify_placed(self, labware: LabwareInstance, mover: IPlateMover, target: str | None = None, site: str | None = None) -> None:
        await self.driver.notify_placed(_handoff(labware, site))

    async def _do_prepare_for_pick(self, labware: LabwareInstance, mover: IPlateMover, target: str | None = None, site: str | None = None) -> None:
        await self.driver.prepare_for_pick(_handoff(labware, site))

    async def _do_notify_picked(self, labware: LabwareInstance, mover: IPlateMover, target: str | None = None, site: str | None = None) -> None:
        await self.driver.notify_picked(_handoff(labware, site))


class _VenusSimulator:
    def build_drivers(
        self, device_type: str, name: str, *, deck_modeling: bool = False,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        return SimulationVenusProtocolDriver(name), SimulationVenusProtocolDriver(name)


def _handoff(labware: LabwareInstance, site: str | None) -> LabwareHandoffRequest:
    return LabwareHandoffRequest(
        labware_name=labware.name,
        labware_type=labware.labware_type,
        site=site,
        barcode=labware.barcode,
    )
