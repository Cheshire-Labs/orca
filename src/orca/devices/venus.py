from dataclasses import dataclass, fields

from cheshire_drivers.protocol_runner_models import LabwareHandoffRequest
from cheshire_drivers.venus_driver import SimulationVenusProtocolDriver, VenusProtocolDriver
from orca.devices.devices import LiquidHandlerProtocol
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placeable_interface import IPlateMover
from orca.runtime.device_factory_context import _get_active_factory, use_device_factory
from orca.runtime.device_factory_protocol import DriverPairElement, IDeviceDriverProvider
from orca.runtime.registries.device_link import is_agent_held
from orca.runtime.run_modes import WorkflowRunMode


class Venus(LiquidHandlerProtocol):
    """A Hamilton liquid handler run by Venus methods.

    Each pick or place on one of its sites runs the matching Venus hook method
    with the labware and the site. Under a device bridge, HxRun.exe, the methods
    folder and the hook methods are set in orca-client's venus settings and must
    not be passed here. Run on the Hamilton PC itself, they are passed here.
    """

    def __init__(
        self,
        name: str,
        *,
        sim_override: WorkflowRunMode | None = None,
        site_names: list[str] | None = None,
        exe_path: str | None = None,
        methods_folder: str | None = None,
        init_protocol: str | None = None,
        prepare_place_protocol: str | None = None,
        placed_protocol: str | None = None,
        prepare_pick_protocol: str | None = None,
        picked_protocol: str | None = None,
    ) -> None:
        settings = _HamiltonPcSettings(
            exe_path, methods_folder, init_protocol,
            prepare_place_protocol, placed_protocol, prepare_pick_protocol, picked_protocol,
        )
        with use_device_factory(_VenusDrivers(settings, _get_active_factory())):
            super().__init__(name, sim_override=sim_override, site_names=site_names)

    async def _do_prepare_for_place(self, labware: LabwareInstance, mover: IPlateMover, target: str | None = None, site: str | None = None) -> None:
        await self.driver.prepare_for_place(_handoff(labware, site))

    async def _do_notify_placed(self, labware: LabwareInstance, mover: IPlateMover, target: str | None = None, site: str | None = None) -> None:
        await self.driver.notify_placed(_handoff(labware, site))

    async def _do_prepare_for_pick(self, labware: LabwareInstance, mover: IPlateMover, target: str | None = None, site: str | None = None) -> None:
        await self.driver.prepare_for_pick(_handoff(labware, site))

    async def _do_notify_picked(self, labware: LabwareInstance, mover: IPlateMover, target: str | None = None, site: str | None = None) -> None:
        await self.driver.notify_picked(_handoff(labware, site))


@dataclass(frozen=True)
class _HamiltonPcSettings:
    """What Venus needs to drive HxRun itself. None means not given."""

    exe_path: str | None
    methods_folder: str | None
    init_protocol: str | None
    prepare_place_protocol: str | None
    placed_protocol: str | None
    prepare_pick_protocol: str | None
    picked_protocol: str | None

    def given(self) -> list[str]:
        return [f.name for f in fields(self) if getattr(self, f.name) is not None]

    def drivers(self, name: str) -> tuple[DriverPairElement, DriverPairElement]:
        exe_path = self.exe_path or r"C:\Program Files (x86)\HAMILTON\Bin\HxRun.exe"
        methods_folder = self.methods_folder or r"C:\Program Files (x86)\HAMILTON\Methods"
        live = VenusProtocolDriver(
            name, init_protocol=self.init_protocol,
            prepare_place_protocol=self.prepare_place_protocol, placed_protocol=self.placed_protocol,
            prepare_pick_protocol=self.prepare_pick_protocol, picked_protocol=self.picked_protocol,
            exe_path=exe_path, methods_folder=methods_folder,
        )
        sim = SimulationVenusProtocolDriver(
            name, init_protocol=self.init_protocol,
            prepare_place_protocol=self.prepare_place_protocol, placed_protocol=self.placed_protocol,
            prepare_pick_protocol=self.prepare_pick_protocol, picked_protocol=self.picked_protocol,
            exe_path=exe_path, methods_folder=methods_folder,
        )
        return live, sim


class _VenusDrivers:
    """Under a device bridge, the bridge's drivers; otherwise HxRun on this PC when settings are given."""

    def __init__(self, settings: _HamiltonPcSettings, bound: IDeviceDriverProvider | None) -> None:
        self._settings = settings
        self._bound = bound

    def build_drivers(
        self, device_type: str, name: str, *, deck_modeling: bool = False,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        if self._bound is None:
            return self._settings.drivers(name)
        live, sim = self._bound.build_drivers(device_type, name)
        given = self._settings.given()
        if is_agent_held(live):
            if given:
                raise ValueError(
                    f"Venus {name!r} runs through the device bridge, which reads "
                    f"{', '.join(given)} from orca-client's venus settings on the Hamilton PC. "
                    f"Set them there and remove them from the topology."
                )
            return live, sim
        return self._settings.drivers(name) if given else (live, sim)


def _handoff(labware: LabwareInstance, site: str | None) -> LabwareHandoffRequest:
    return LabwareHandoffRequest(
        labware_name=labware.name,
        labware_type=labware.labware_type,
        site=site,
        barcode=labware.barcode,
    )
