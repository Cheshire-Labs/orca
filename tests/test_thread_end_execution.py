"""A thread's `end=` parameter is carried out when the thread terminates,
including when a `while ctx.has_more_work()` receiver loop exits on quiescence.

The end has two effects, both pinned here against real post-run state:
- a MOVE to the end location (when it differs from where the thread ran), and
- a dispose-or-leave at that location: `LEAVE_IN_PLACE` keeps the labware, the
  bare-string / `MANUAL_REMOVE` default disposes it.

"Dispose" means the end strategy ran (the end location cleared), not routing to
waste specifically. A non-leave end must target a top-level location: the
runtime guards a deck-site end without LEAVE_IN_PLACE, because completion routes
to the device handoff, not the site, so a site dispose would no-op.
"""

import pytest

import orca.orca as orca
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, REUSE_EXISTING
from cheshire_drivers import (
    CartesianCoordinates as C,
    DeckLayoutConfig,
    DeckResourceConfig,
    RecordingLiquidHandlerDriver,
    Teachpoint,
)
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from tests.test_helpers import RecordingLhDeckFactory, run_to_quiescence


DECK_CONFIG = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        DeckResourceConfig(name="carrier-25", catalog_ref="Trough_CAR_4R200_A00", rail=25),
    ],
)

RESERVOIR_SITE = "lh/carrier-25-0"


async def _build_end(*, reservoir_end, plate_end):
    """A transit plate consumes a deck-resident reagent once. `reservoir_end`
    sets the resident reagent receiver's `end=`; `plate_end` sets the transit
    plate thread's `end=`."""
    stores = InMemoryRuntimeStoreFactory()

    sample_plate = PlateTemplate(
        "sample_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    reservoir = PlateTemplate(
        "reservoir", labware_type="AGenBio_1_troughplate_190000uL_Fl")

    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    with use_device_factory(RecordingLhDeckFactory(recorder)):
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

    @orca.action(
        device=lh,
        inputs=[sample_plate, reservoir],
        deck_positions={sample_plate: "carrier-7-0"},
    )
    async def use_reagent(ctx: ActionContext) -> None:
        ctx.labware("reservoir")

    @orca.method
    async def use_reagent_method(ctx: MethodContext):
        yield use_reagent

    @orca.thread(labware=sample_plate, start=("stacker", DISPENSE), end=plate_end)
    async def plate_journey(ctx: ThreadContext):
        yield use_reagent_method

    @orca.thread(
        labware=reservoir,
        start=(RESERVOIR_SITE, REUSE_EXISTING),
        end=reservoir_end,
    )
    async def reservoir_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[use_reagent_method])

    @orca.workflow(name="end_exec_wf")
    def workflow(wf):
        wf.start(plate_journey)
        wf.thread(reservoir_journey)

    topology = Topology(
        locations={"stacker": stacker, "pad": pad, "lh": lh, "waste": waste},
        transporters=[arm],
    )
    return await orca.build_system(
        name="End Exec", workflow=workflow, topology=topology, stores=stores)


async def _start_and_run(build) -> tuple[SystemRuntime, dict[str, str]]:
    """Start the runtime, submit, and poll to quiescence. The caller owns
    shutdown in a finally so a failing assertion never leaks a live runtime."""
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    record = await runtime.submit_workflow("end_exec_wf", mode=WorkflowRunMode.PURE_SIM)
    statuses = await run_to_quiescence(runtime, record.id)
    return runtime, statuses


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_leave_in_place_keeps_resident_on_its_site() -> None:
    """The headline no-auto-dispose case: a deck-resident receiver ending at its
    own site with LEAVE_IN_PLACE stays put when its `has_more_work()` loop exits
    on quiescence. This persistence is what lets the next submission's reuse-bind
    path re-attach to the same physical labware."""
    build = await _build_end(
        reservoir_end=(RESERVOIR_SITE, LEAVE_IN_PLACE), plate_end="waste")
    runtime, statuses = await _start_and_run(build)
    try:
        assert all(s == "COMPLETED" for s in statuses.values()), statuses
        site = build.system.get_location(RESERVOIR_SITE)
        assert site.labware is not None, (
            "LEAVE_IN_PLACE must skip dispose: the resident reagent must still be "
            "on its deck site after the receiver loop exits on quiescence")
        assert site.labware.template_name == "reservoir"
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_transit_thread_executes_end_move() -> None:
    """The MOVE half of end execution: a transit plate whose end is a different
    location than where the action ran arrives there. Paired with LEAVE_IN_PLACE
    so the plate is observable at the destination (the move ran, no dispose).
    Establishes arrival for the dispose test below."""
    build = await _build_end(
        reservoir_end=(RESERVOIR_SITE, LEAVE_IN_PLACE),
        plate_end=("pad", LEAVE_IN_PLACE),
    )
    runtime, statuses = await _start_and_run(build)
    try:
        assert all(s == "COMPLETED" for s in statuses.values()), statuses
        pad = build.system.get_location("pad")
        assert pad.labware is not None, (
            "the transit thread's end move must execute: the plate must arrive at "
            "pad, where LEAVE_IN_PLACE keeps it")
        assert pad.labware.template_name == "sample_plate"
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_transit_thread_end_dispose_executes() -> None:
    """The DISPOSE half: the same transit plate ends at the same top-level
    location with the bare-string / MANUAL_REMOVE default (PURE_SIM disposes
    without waiting for an operator), so after completion the end location is
    cleared. Identical flow to the move test, opposite outcome, proving the end
    strategy drives the behavior, and that dispose means 'execute the end', not
    routing to waste specifically."""
    build = await _build_end(
        reservoir_end=(RESERVOIR_SITE, LEAVE_IN_PLACE), plate_end="pad")
    runtime, statuses = await _start_and_run(build)
    try:
        assert all(s == "COMPLETED" for s in statuses.values()), statuses
        assert build.system.get_location("pad").labware is None, (
            "the transit thread's disposing end must execute: the plate is moved "
            "to pad and disposed, clearing it")
    finally:
        await runtime.shutdown()
