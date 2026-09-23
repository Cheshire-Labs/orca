"""A discharge is a recovery verb, so a dead liquid handler must not block it.

The clear that takes labware off every deck world reads each world's driver to
find out whether it holds the plate. On a bench that read goes over the wire,
and the operator reaching for a discharge is often reaching for it BECAUSE the
handler is down. A clear that refuses then leaves them with nothing: the ledger
keeps the row, the slot stays claimed, and the only way out is a restart.

So the driver half of a wipe is best-effort and runs after the stores are
written. What the engine knows is corrected whatever the instrument says.
"""

import pytest

import orca.orca as orca
from cheshire_drivers import CartesianCoordinates as C
from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig, Teachpoint
from cheshire_drivers.liquid_handler_models import (
    GetDeckStateRequest,
    LabwareStateResponse,
    ResetDeckLabwareRequest,
)
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from orca.devices.devices import LiquidHandler
from orca.gateway.controller.exceptions import DeviceOfflineError
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.device_factory_protocol import DriverPairElement
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.run_modes import WorkflowRunMode, mode_scope
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate

DECK = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
    ],
)

DECK_SITE = "lh/carrier-7-0"
PAD = "pad"


class _DriverThatCanGoDown(ChatterboxLiquidHandlerDriver):
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

    async def get_deck_state(self, request: GetDeckStateRequest) -> LabwareStateResponse:
        self._refuse_if_down()
        return await super().get_deck_state(request)

    async def configure_deck(self, config: DeckLayoutConfig) -> None:
        self._refuse_if_down()
        await super().configure_deck(config)

    async def reset_deck_labware(self, request: ResetDeckLabwareRequest) -> None:
        self._refuse_if_down()
        await super().reset_deck_labware(request)


class _SplitFactory:
    def __init__(
        self, live: ChatterboxLiquidHandlerDriver, sim: ChatterboxLiquidHandlerDriver,
    ) -> None:
        self._live = live
        self._sim = sim
        from orca.runtime.device_factory import SimDeviceFactory
        self._fallback = SimDeviceFactory()

    def build_drivers(
        self, device_type: str, name: str, *, deck_modeling: bool = False,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        if device_type == "liquid_handler":
            return self._live, self._sim
        return self._fallback.build_drivers(device_type, name, deck_modeling=deck_modeling)


async def _runtime_with_a_plate_on_the_sim_deck(
    live: _DriverThatCanGoDown, sim: _DriverThatCanGoDown,
):
    """A handler whose LIVE world has been laid out and whose SIM world holds a
    plate: what a bench looks like after a connect and a sim run."""
    stores = InMemoryRuntimeStoreFactory()
    plate = PlateTemplate(
        "sample_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")

    with use_device_factory(_SplitFactory(live, sim)):
        lh = LiquidHandler(
            "lh",
            deck_layout_store=stores.deck_layouts("lh", seed={"default": DECK}),
            deck_layout="default",
        )
        pad = PlatePad(PAD)
        arm = Transporter(
            "arm",
            teachpoint_store=stores.teachpoints("arm", seed=[
                Teachpoint(PAD, C(0, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint(DECK_SITE, C(200, 200, 300, 0, 90, 180), orientation="right"),
            ]),
        )

    build = await orca.build_system(
        name="Handler Down",
        topology=Topology(locations={"lh": lh, PAD: pad}, transporters=[arm]),
        stores=stores,
        labwares=[plate],
    )
    runtime = SystemRuntime(
        build.system, event_bus=build.event_bus, labware_store=InMemoryLabwareStore(),
    )
    await runtime.start()
    # An operator register resolves to LIVE, which lays that world out. The
    # plate reaches the sim world the way a sim run puts it there.
    registered = await runtime.labware.register(
        "sample_plate", location=DECK_SITE, confirm=True)
    instance = next(lw for lw in build.system.labwares if lw.id == registered.id)
    site = build.system.system_map.resolve_placement_location(DECK_SITE)
    with mode_scope(WorkflowRunMode.PURE_SIM):
        await build.system.project_labware_on_lh_decks(instance, site)
    return runtime, registered


async def _deck_names(driver: _DriverThatCanGoDown) -> set[str]:
    was_down, driver.down = driver.down, False
    try:
        state = await driver.get_deck_state(GetDeckStateRequest())
    finally:
        driver.down = was_down
    return {item.name for item in state.labware}


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

        await runtime.labware.discharge_labware(registered.id, force=True)

        assert not await runtime.labware.list_all(), (
            "the ledger kept the row, so the operator has no way to free the slot"
        )
        assert registered.name not in await _deck_names(sim), (
            "the world that was reachable kept the plate because an unreachable "
            "one raised first"
        )
    finally:
        await runtime.shutdown()


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

        cleared = await runtime.labware.clear_all_labware(force=True)

        assert registered.id in cleared, f"clear-all reported {cleared}"
        assert not await runtime.labware.list_all(), "the ledger still holds rows"
        assert registered.name not in await _deck_names(sim), (
            "the reachable sim deck kept the plate"
        )
    finally:
        await runtime.shutdown()
