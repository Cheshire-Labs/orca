"""A thread may start/end at a deck-site child location (e.g. "lh/carrier-25-0"),
not only at a top-level Topology location.

Motivation: a deck-resident reagent (reservoir, calibration plate) on a liquid
handler should be pinnable to a SPECIFIC deck site, so it does not squat the
deck site the arm delivers transit labware to. The deck derives flat routing
nodes named "<device>/<carrier>-<site_index>"; this test asserts those names
resolve as thread start/end locations.
"""

import pytest

import orca.orca as orca
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, MANUAL_REMOVE, REUSE_EXISTING
from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig, Teachpoint
from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.deck_site import DeckSite
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory_protocol import DriverPairElement
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import SystemBuild, Topology
from orca.sdk.labware import PlateTemplate
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import (
    DeckSiteEndRequiresLeaveInPlaceError,
    EndArg,
    ThreadTemplate,
)
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.test_helpers import run_to_quiescence


DECK_CONFIG = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        DeckResourceConfig(name="carrier-25", catalog_ref="Trough_CAR_4R200_A00", rail=25),
    ],
)

# The arm reaches exactly this deck site; the gripper relays onward from it.
ARM_DECK_ENTRY = "lh/carrier-7-2"


async def _build(
    recorder: RecordingLiquidHandlerDriver,
    reservoir_end: EndArg = ("lh/carrier-25-0", LEAVE_IN_PLACE),
) -> tuple[SystemBuild, ThreadTemplate, WorkflowTemplate]:
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

        def build_drivers(
            self, device_type: str, name: str, *, deck_modeling: bool = False,
        ) -> tuple[DriverPairElement, DriverPairElement]:
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

        from cheshire_drivers import CartesianCoordinates as C
        arm = Transporter(
            "arm",
            teachpoint_store=stores.teachpoints("arm", seed=[
                Teachpoint("stacker", C(0, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("pad", C(200, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint(ARM_DECK_ENTRY, C(400, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("waste", C(600, 200, 300, 0, 90, 180), orientation="right"),
            ]),
        )

    # Reservoir is deck-resident at carrier-25-0; the transit sample_plate is
    # routed to its own site (carrier-7-0) via deck_positions. Body is a no-op.
    @orca.action(
        device=lh,
        inputs=[sample_plate, reservoir],
        deck_positions={sample_plate: "carrier-7-0"},
    )
    async def add_reagent(ctx: ActionContext) -> None:
        ctx.labware("reservoir")
        ctx.labware("sample_plate")

    @orca.method
    async def add_reagent_method(ctx: MethodContext):
        yield add_reagent

    @orca.thread(labware=sample_plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield add_reagent_method

    # The feature under test: start (and end) at a deck-site child location.
    @orca.thread(
        labware=reservoir,
        start=("lh/carrier-25-0", REUSE_EXISTING),
        end=reservoir_end,
    )
    async def reservoir_journey(ctx: ThreadContext):
        yield orca.join(allows=[add_reagent_method])

    @orca.workflow(name="deck_site_start_wf")
    def workflow(wf):
        wf.start(plate_journey)
        wf.thread(reservoir_journey)

    topology = Topology(
        locations={"stacker": stacker, "pad": pad, "lh": lh, "waste": waste},
        transporters=[arm],
    )
    build = await orca.build_system(
        name="Deck Site Start", workflow=workflow, topology=topology, stores=stores)
    return build, reservoir_journey, workflow


async def test_thread_can_start_at_a_deck_site_child_location() -> None:
    inner = ChatterboxLiquidHandlerDriver(num_channels=8)
    recorder = RecordingLiquidHandlerDriver(inner)

    # Pre-fix: build_system raises KeyError resolving "lh/carrier-25-0"
    # (child deck sites are not graph nodes, so get_location rejects them).
    build, reservoir_journey, _ = await _build(recorder)

    start_loc = reservoir_journey.start_location
    assert start_loc.position_id == "lh/carrier-25-0", (
        f"expected deck-site child location, got {start_loc.position_id!r}"
    )
    assert isinstance(start_loc.resource, DeckSite), (
        f"expected a DeckSite resource, got {type(start_loc.resource).__name__}"
    )


async def test_deck_site_end_without_leave_in_place_is_rejected() -> None:
    """A deck-site end is only coherent as LEAVE_IN_PLACE. Completion disposal
    does not route labware back to the named deck site, so a non-LEAVE end
    would dispose against an empty site and the labware would never clear.
    build_system must refuse the combination at build time."""
    inner = ChatterboxLiquidHandlerDriver(num_channels=8)
    recorder = RecordingLiquidHandlerDriver(inner)

    with pytest.raises(DeckSiteEndRequiresLeaveInPlaceError) as exc:
        await _build(recorder, reservoir_end=("lh/carrier-25-0", MANUAL_REMOVE))
    assert exc.value.position_id == "lh/carrier-25-0"


async def test_deck_site_among_end_candidates_is_rejected() -> None:
    """The guard must run on EVERY candidate, not just a lone end.

    Candidate lists made the guard a loop, and the loop briefly carried an
    isinstance that skipped rather than raised. A deck site hidden among
    otherwise-fine candidates is the shape that would slip through.
    """
    inner = ChatterboxLiquidHandlerDriver(num_channels=8)
    recorder = RecordingLiquidHandlerDriver(inner)

    with pytest.raises(DeckSiteEndRequiresLeaveInPlaceError) as exc:
        await _build(
            recorder,
            reservoir_end=(["pad", "lh/carrier-25-0"], MANUAL_REMOVE),
        )
    assert exc.value.position_id == "lh/carrier-25-0"


@pytest.mark.slow
@pytest.mark.asyncio
async def test_deck_site_resident_reservoir_runs_in_sim() -> None:
    """End-to-end: a REUSE_EXISTING reservoir pinned to a deck site co-locates
    with the transit plate (routed to its own site via deck_positions) and the
    workflow completes. Proves the deck-site start binds, the arm's deck entry
    site stays free, and the reservoir does not wedge at CREATED.

    Runs through SystemRuntime (not SystemBuild.run, whose source-available WorkflowExecutor
    path does not plumb the labware_store the reuse-bind needs)."""
    inner = ChatterboxLiquidHandlerDriver(num_channels=8)
    recorder = RecordingLiquidHandlerDriver(inner)

    build, _, _ = await _build(recorder)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    record = await runtime.submit_workflow(
        "deck_site_start_wf", mode=WorkflowRunMode.PURE_SIM)

    statuses = await run_to_quiescence(runtime, record.id)

    assert statuses, "no threads were created"
    assert all(s == "COMPLETED" for s in statuses.values()), (
        f"expected all threads COMPLETED, got {statuses}"
    )
