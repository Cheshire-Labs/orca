"""A discharge and a clear-all are recovery verbs, so a dead liquid handler must not block either.

The operator reaching for one is often reaching for it BECAUSE the handler is down. A clear that refuses leaves them
with nothing: the ledger keeps the row, the slot stays claimed, and the only way out is a restart. So the stores are
written first. A discharge then marks every deck world, and the next command or deck read rebuilds a world from the
record. A clear-all also resets each world that answers, then marks them all.
"""

from orca.events.event_bus import EventBus
import pytest

import orca.orca as orca
from cheshire_drivers import CartesianCoordinates as C
from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig, Teachpoint
from cheshire_drivers.liquid_handler_models import (
    DeckStateResponse,
    GetDeckStateRequest,
    LabwareStateResponse,
    ResetDeckLabwareRequest,
)
from cheshire_drivers.plr import ChatterboxLiquidHandlerWithProtocolDriver
from orca.devices.devices import LiquidHandler
from orca.gateway.controller.exceptions import DeviceOfflineError
from orca.gateway.operator_commands import OperatorDeviceCommands
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.runtime_interface import ISystemRuntime
from orca.modes.run_modes import WorkflowRunMode, mode_scope
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from tests.setup_package import package_stores, write_deck_layouts, write_teachpoints
from tests.setup_package import with_teachpoints, with_deck_layouts
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.one_deck_per_world import OneDeckPerWorld
from orca.modes.device_modes import DeviceMode
from tests.device_modes import set_to
from tests.real_device_path import the_real_device_path

DECK = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
    ],
)

DECK_SITE = "lh/carrier-7-0"
PAD = "pad"


class _DriverThatCanGoDown(ChatterboxLiquidHandlerWithProtocolDriver):
    """A handler that stops answering partway through the test.

    Subclasses the real Chatterbox so everything before the outage behaves the
    way it does in the other deck tests; only the answering stops.
    """

    def __init__(self) -> None:
        super().__init__(num_channels=8)
        self.down = False

    def _refuse_if_down(self) -> None:
        if self.down:
            raise DeviceOfflineError("lh is not connected")

    async def get_deck_state(self, request: GetDeckStateRequest) -> DeckStateResponse:
        self._refuse_if_down()
        return await super().get_deck_state(request)

    async def configure_deck(self, config: DeckLayoutConfig) -> LabwareStateResponse:
        self._refuse_if_down()
        return await super().configure_deck(config)

    async def reset_deck_labware(self, request: ResetDeckLabwareRequest) -> LabwareStateResponse:
        self._refuse_if_down()
        return await super().reset_deck_labware(request)


async def _runtime_with_a_plate_on_the_sim_deck(
    live: _DriverThatCanGoDown, sim: _DriverThatCanGoDown,
):
    """A handler whose LIVE world has been laid out and whose SIM world holds a
    plate: what a bench looks like after a connect and a sim run."""
    stores = package_stores()
    plate = PlateTemplate(
        "sample_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")

    the_real_device_path().simulate_with("lh", OneDeckPerWorld(live=live, sim=sim))
    lh = with_deck_layouts(LiquidHandler(
        "lh",
        deck_layout="default",
    ), write_deck_layouts(stores, "lh", {"default": DECK}))
    set_to(lh, "lh", DeviceMode.LIVE)
    pad = PlatePad(PAD)
    arm = with_teachpoints(Transporter(
        "arm",
    ), write_teachpoints(stores, "arm", [
            Teachpoint(PAD, C(0, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint(DECK_SITE, C(200, 200, 300, 0, 90, 180), orientation="right"),
        ]))

    build = await orca.build_system(
        name="Handler Down",
        topology=Topology(locations={"lh": lh, PAD: pad}, transporters=[arm]),
        stores=stores,
        labwares=[plate], event_bus=EventBus(), ops_history_store=memory_ops_store(),
    )
    runtime = await memory_runtime(
        build.system, event_bus=build.event_bus
    )
    await runtime.start()
    # An operator register resolves to LIVE, which lays that world out. The
    # plate reaches the sim world the way a sim run puts it there.
    registered = await runtime.labware.register(
        "sample_plate", location=DECK_SITE, confirm=True)
    instance = next(lw for lw in build.system.labwares if lw.id == registered.id)
    site = build.system.system_map.resolve_placement_location(DECK_SITE)
    with mode_scope(WorkflowRunMode.SIM):
        await build.system.project_labware_on_lh_decks(instance, site)
    return runtime, registered


async def _what_an_operator_reads_on_the_sim_deck(runtime: ISystemRuntime) -> set[str]:
    """An operator's own deck read while a sim run is going, which resolves to the sim world."""
    with mode_scope(WorkflowRunMode.SIM):
        read = await OperatorDeviceCommands(the_real_device_path().gateway, lambda: runtime).run_named(
            "lh", "get_deck_state", {},
        )
    assert isinstance(read.value, dict), read.value
    labware = read.value["labware"]
    assert isinstance(labware, list), labware
    return {str(item["name"]) for item in labware if isinstance(item, dict)}


async def _deck_names(driver: _DriverThatCanGoDown) -> set[str]:
    was_down, driver.down = driver.down, False
    try:
        state = await driver.get_deck_state(GetDeckStateRequest())
    finally:
        driver.down = was_down
    return {item.name for item in state.labware}


@pytest.mark.out_of_run
@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_discharge_completes_while_the_handler_is_down() -> None:
    live = _DriverThatCanGoDown()
    sim = _DriverThatCanGoDown()
    runtime, registered = await _runtime_with_a_plate_on_the_sim_deck(live, sim)
    try:
        assert registered.name in await _deck_names(sim)
        live.down = True

        await runtime.labware.discharge_labware(registered.id, force=True, confirm=True)

        assert not await runtime.labware.list_all(), (
            "the ledger kept the row, so the operator has no way to free the slot"
        )
        assert registered.name not in await _what_an_operator_reads_on_the_sim_deck(runtime), (
            "an operator reading the reachable deck sees the plate the discharge removed"
        )
    finally:
        await runtime.shutdown()


@pytest.mark.out_of_run
@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_clear_all_completes_while_the_handler_is_down() -> None:
    """The panic button most of all. It is what an operator reaches for when the
    deck is wrong, which is exactly when a handler is likely to be down."""
    live = _DriverThatCanGoDown()
    sim = _DriverThatCanGoDown()
    runtime, registered = await _runtime_with_a_plate_on_the_sim_deck(live, sim)
    try:
        live.down = True

        cleared = await runtime.labware.clear_all_labware(force=True, confirm=True)

        assert registered.id in cleared, f"clear-all reported {cleared}"
        assert not await runtime.labware.list_all(), "the ledger still holds rows"
        assert registered.name not in await _deck_names(sim), (
            "the reachable sim deck kept the plate"
        )
    finally:
        await runtime.shutdown()
