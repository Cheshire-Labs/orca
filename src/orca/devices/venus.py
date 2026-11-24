from cheshire_drivers.protocol_runner_models import LabwareHandoffRequest
from orca.devices.devices import LiquidHandlerProtocol
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placeable_interface import IPlateMover


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
        site_names: list[str] | None = None,
    ) -> None:
        super().__init__(name, site_names=site_names)

    async def _do_prepare_for_place(self, labware: LabwareInstance | None, mover: IPlateMover, target: str | None = None, site: str | None = None) -> None:
        await self.engine_commands.run("prepare_for_place", lambda: self.driver.prepare_for_place(_handoff(labware, site)))

    async def _do_notify_placed(self, labware: LabwareInstance | None, mover: IPlateMover, target: str | None = None, site: str | None = None) -> None:
        await self.engine_commands.run("notify_placed", lambda: self.driver.notify_placed(_handoff(labware, site)))

    async def _do_prepare_for_pick(self, labware: LabwareInstance | None, mover: IPlateMover, target: str | None = None, site: str | None = None) -> None:
        await self.engine_commands.run("prepare_for_pick", lambda: self.driver.prepare_for_pick(_handoff(labware, site)))

    async def _do_notify_picked(self, labware: LabwareInstance | None, mover: IPlateMover, target: str | None = None, site: str | None = None) -> None:
        await self.engine_commands.run("notify_picked", lambda: self.driver.notify_picked(_handoff(labware, site)))


def _handoff(labware: LabwareInstance | None, site: str | None) -> LabwareHandoffRequest:
    if labware is None:
        return LabwareHandoffRequest(site=site)
    return LabwareHandoffRequest(
        labware_name=labware.name,
        labware_type=labware.labware_type,
        site=site,
        barcode=labware.barcode,
    )
