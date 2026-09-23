"""The placement chokepoint: every path that records a plate at a location
writes ALL the holders the downstream consumers read, in one place.

Bug 1 (deck-site placement): a plate placed on a liquid-handler deck-site CHILD
(``lh/carrier-7-0``) was written only to the child slot + the position ledger.
The bridge loaded-list (what the pick reads) and the transporter world graph
(what routing reads) were never populated, so the first move off the deck
raised ``not found in loaded labwares`` / ``position 'lh' is empty``.

These tests drive placement through the operator surface (``register``) and an
end-to-end run that starts on a deck site and moves off, and assert the
pick-side holders (driver-deck projection, site slot) are populated and the
pick succeeds.
"""

import pytest

import orca.orca as orca
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, REUSE_EXISTING
from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig, Teachpoint
from cheshire_drivers.liquid_handler_models import GetDeckStateRequest
from cheshire_drivers import CartesianCoordinates as C
from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from tests.test_helpers import run_to_quiescence


SAMPLE_START = "lh/carrier-7-0"
RESERVOIR_SITE = "lh/carrier-25-0"

DECK_CONFIG = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        DeckResourceConfig(name="carrier-25", catalog_ref="Trough_CAR_4R200_A00", rail=25),
    ],
)


async def _build(recorder: RecordingLiquidHandlerDriver, *, wf_name: str, sample_start):
    stores = InMemoryRuntimeStoreFactory()

    sample_plate = PlateTemplate(
        "sample_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    reservoir = PlateTemplate(
        "reservoir", labware_type="AGenBio_1_troughplate_190000uL_Fl")

    class _LhDeckFactory:
        def __init__(self, lh: RecordingLiquidHandlerDriver) -> None:
            self._lh = lh
            from orca.runtime.device_factory import SimDeviceFactory
            self._fallback = SimDeviceFactory()

        def build_drivers(self, device_type: str, name: str, *, deck_modeling: bool = False):
            if device_type == "liquid_handler":
                return self._lh, self._lh
            return self._fallback.build_drivers(device_type, name, deck_modeling=deck_modeling)

    with use_device_factory(_LhDeckFactory(recorder)):
        lh = LiquidHandler(
            "lh",
            deck_layout_store=stores.deck_layouts("lh", seed={"default": DECK_CONFIG}),
            deck_layout="default",
        )
        stacker = Storage("stacker")
        waste = Storage("waste")
        pad = PlatePad("pad")
        arm = Transporter(
            "arm",
            teachpoint_store=stores.teachpoints("arm", seed=[
                Teachpoint("stacker", C(0, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("pad", C(200, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("lh/carrier-7-2", C(400, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("waste", C(600, 200, 300, 0, 90, 180), orientation="right"),
            ]),
        )

    @orca.action(device=lh, inputs=[sample_plate])
    async def touch_plate(ctx: ActionContext) -> None:
        ctx.labware("sample_plate")

    @orca.method
    async def touch_method(ctx: MethodContext):
        yield touch_plate

    # Picking the plate off the deck child to route to waste is the Bug 1 point.
    @orca.thread(labware=sample_plate, start=sample_start, end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield touch_method

    @orca.workflow(name=wf_name)
    def workflow(wf):
        wf.start(plate_journey)

    topology = Topology(
        locations={"stacker": stacker, "pad": pad, "lh": lh, "waste": waste},
        transporters=[arm],
    )
    build = await orca.build_system(
        name="Placement Chokepoint", workflow=workflow, topology=topology, stores=stores)
    return build, lh


@pytest.mark.asyncio
async def test_register_to_deck_site_populates_pick_holders() -> None:
    """Registering a plate at a deck site must populate the holders the pick
    path reads: the flat site node's slot and the LH driver-deck projection.
    Pre-fix register wrote only the slot, so the pick-side holders stayed
    empty and the first move off the deck raised."""
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    store = InMemoryLabwareStore()
    build, _ = await _build(recorder, wf_name="chokepoint_register", sample_start=("stacker", DISPENSE))
    runtime = SystemRuntime(build.system, event_bus=build.event_bus, labware_store=store)
    await runtime.start()

    await runtime.labware.register("sample_plate", location=SAMPLE_START, confirm=True)

    resident = build.system.system_map.get_location(SAMPLE_START).labware
    assert resident is not None, "register did not write the deck-site slot"
    deck_state = await recorder.get_deck_state(GetDeckStateRequest())
    deck_names = {resource.name for resource in deck_state.labware}
    assert resident.name in deck_names, (
        "driver deck projection missing the deck-site resident after register; "
        "the pick path will not find the plate on the deck"
    )


@pytest.mark.slow
@pytest.mark.asyncio
async def test_thread_starting_on_deck_site_is_picked_off_and_completes() -> None:
    """End-to-end Bug 1 repro: a sample plate starts on a deck site
    (``lh/carrier-7-0``) via the default MANUAL_PLACE spawn, runs an LH action,
    and is then picked off the deck and routed to waste. Pre-fix the pick off
    the deck raised because the bridge loaded-list and transporter graph were
    never seeded at placement."""
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    store = InMemoryLabwareStore()
    build, _ = await _build(recorder, wf_name="chokepoint_e2e", sample_start=SAMPLE_START)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus, labware_store=store)
    await runtime.start()

    record = await runtime.submit_workflow("chokepoint_e2e", mode=WorkflowRunMode.PURE_SIM)
    statuses = await run_to_quiescence(runtime, record.id)

    assert statuses, "no threads were created"
    assert all(s == "COMPLETED" for s in statuses.values()), (
        f"expected all threads COMPLETED, got {statuses}"
    )
