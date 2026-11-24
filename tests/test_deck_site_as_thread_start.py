"""A thread may start/end at a deck-site child location (e.g. "lh/carrier-25-0"),
not only at a top-level Topology location.

Motivation: a deck-resident reagent (reservoir, calibration plate) on a liquid
handler should be pinnable to a SPECIFIC deck site, so it does not squat the
deck site the arm delivers transit labware to. The deck derives flat routing
nodes named "<device>/<carrier>-<site_index>"; this test asserts those names
resolve as thread start/end locations.
"""

from orca.events.event_bus import EventBus
import pytest

from tests.recording_handler import RecordingHandler

import orca.orca as orca
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, MANUAL_REMOVE, REUSE_EXISTING
from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig, Teachpoint
from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.plr import ChatterboxLiquidHandlerWithProtocolDriver
from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.deck_site import DeckSite
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.modes.run_modes import WorkflowRunMode
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
from tests.setup_package import package_stores, write_deck_layouts, write_teachpoints
from tests.setup_package import with_teachpoints, with_deck_layouts
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.real_device_path import the_real_device_path


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
    stores = package_stores()

    sample_plate = PlateTemplate(
        "sample_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    reservoir = PlateTemplate(
        "reservoir", labware_type="AGenBio_1_troughplate_190000uL_Fl")

    the_real_device_path().simulate_with("lh", recorder)
    lh = with_deck_layouts(LiquidHandler(
        "lh",
        deck_layout="default",
    ), write_deck_layouts(stores, "lh", {"default": DECK_CONFIG}))
    stacker = Storage("stacker")
    waste = Storage("waste")
    pad = PlatePad("pad")

    from cheshire_drivers import CartesianCoordinates as C
    arm = with_teachpoints(Transporter(
        "arm",
    ), write_teachpoints(stores, "arm", [
            Teachpoint("stacker", C(0, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint("pad", C(200, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint(ARM_DECK_ENTRY, C(400, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint("waste", C(600, 200, 300, 0, 90, 180), orientation="right"),
        ]))

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
        name="Deck Site Start", workflow=workflow, topology=topology, stores=stores, event_bus=EventBus(), ops_history_store=memory_ops_store())
    return build, reservoir_journey, workflow


async def test_thread_can_start_at_a_deck_site_child_location() -> None:
    inner = ChatterboxLiquidHandlerWithProtocolDriver(num_channels=8)
    recorder = RecordingHandler(inner)

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
    inner = ChatterboxLiquidHandlerWithProtocolDriver(num_channels=8)
    recorder = RecordingHandler(inner)

    with pytest.raises(DeckSiteEndRequiresLeaveInPlaceError) as exc:
        await _build(recorder, reservoir_end=("lh/carrier-25-0", MANUAL_REMOVE))
    assert exc.value.position_id == "lh/carrier-25-0"


async def test_deck_site_among_end_candidates_is_rejected() -> None:
    """The guard must run on EVERY candidate, not just a lone end.

    Candidate lists made the guard a loop, and the loop briefly carried an
    isinstance that skipped rather than raised. A deck site hidden among
    otherwise-fine candidates is the shape that would slip through.
    """
    inner = ChatterboxLiquidHandlerWithProtocolDriver(num_channels=8)
    recorder = RecordingHandler(inner)

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
    site stays free, and the reservoir does not wedge at CREATED."""
    inner = ChatterboxLiquidHandlerWithProtocolDriver(num_channels=8)
    recorder = RecordingHandler(inner)

    build, _, _ = await _build(recorder)
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    record = await runtime.submit_workflow(
        "deck_site_start_wf", mode=WorkflowRunMode.CHECK_RUN)

    statuses = await run_to_quiescence(runtime, record.id)

    assert statuses, "no threads were created"
    assert all(s == "COMPLETED" for s in statuses.values()), (
        f"expected all threads COMPLETED, got {statuses}"
    )
