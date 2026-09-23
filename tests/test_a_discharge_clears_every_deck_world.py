"""A discharge takes the labware off whichever driver deck actually holds it.

On the bench five plates were discharged, each answering "discharged", and the
ledger went empty. The next run was then refused at three slots, each refusal
naming one of the five as the occupant, and only a restart cleared them.

The plates had been put down by a PURE_SIM run, so they lived in the sim
driver's deck world. An operator discharge resolves to LIVE, so the clear was
applied to a different world and the sim world kept them. Engine and driver
disagreeing about what is standing on a deck is how an arm gets routed into an
occupied site, so the clear has to reach the world the labware is really in.

Reaching it also means finding it. A thread that ends retires its labware and
frees the slot, so a run that finished leaves the discharge nothing to match on
and the plate stands on the driver deck untouched. The clear asks the handlers
instead of asking the engine where the plate was.
"""

import pytest

import orca.orca as orca
from cheshire_drivers import CartesianCoordinates as C
from cheshire_drivers import (
    DeckLayoutConfig,
    DeckResourceConfig,
    RecordingLiquidHandlerDriver,
    Teachpoint,
)
from cheshire_drivers.liquid_handler_models import (
    AddDeckLabwareRequest,
    GetDeckStateRequest,
)
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from orca.devices.device_interfaces import ILiquidHandler
from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.device_factory_protocol import DriverPairElement
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.run_modes import WorkflowRunMode, mode_scope
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate, TipRackTemplate
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, REUSE_EXISTING
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from tests.test_helpers import named_for_template, run_to_quiescence

DECK_CONFIG = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        DeckResourceConfig(name="carrier-25", catalog_ref="Trough_CAR_4R200_A00", rail=25),
    ],
)

RESERVOIR_SITE = "lh/carrier-25-0"
NEIGHBOUR_SITE = "lh/carrier-25-1"
ARM_DECK_ENTRY = "lh/carrier-7-2"


class _SplitLhFactory:
    """Give the liquid handler two DIFFERENT drivers, one per world.

    The real factory does this too. Tests that hand the same recorder to both
    slots collapse the sim and live worlds into one dict, and a clear applied
    to the wrong world then looks like it worked.
    """

    def __init__(
        self, live: RecordingLiquidHandlerDriver, sim: RecordingLiquidHandlerDriver,
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


async def _build(
    live: RecordingLiquidHandlerDriver, sim: RecordingLiquidHandlerDriver,
):
    """A workflow that parks a reservoir on a liquid-handler deck site and
    leaves it there, so a run ends with a plate standing on the driver deck."""
    stores = InMemoryRuntimeStoreFactory()
    sample_plate = PlateTemplate(
        "sample_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    reservoir = PlateTemplate(
        "reservoir", labware_type="AGenBio_1_troughplate_190000uL_Fl")
    neighbour = PlateTemplate(
        "neighbour", labware_type="AGenBio_1_troughplate_190000uL_Fl")

    with use_device_factory(_SplitLhFactory(live, sim)):
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
                Teachpoint(ARM_DECK_ENTRY, C(400, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("waste", C(600, 200, 300, 0, 90, 180), orientation="right"),
            ]),
        )

    @orca.action(
        device=lh,
        inputs=[sample_plate, reservoir, neighbour],
        deck_positions={sample_plate: "carrier-7-0"},
    )
    async def add_reagent(ctx: ActionContext) -> None:
        ctx.labware("reservoir")
        ctx.labware("neighbour")

    @orca.method
    async def add_reagent_method(ctx: MethodContext):
        yield add_reagent

    @orca.thread(labware=sample_plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield add_reagent_method

    @orca.thread(
        labware=reservoir,
        start=(RESERVOIR_SITE, REUSE_EXISTING),
        end=(RESERVOIR_SITE, LEAVE_IN_PLACE),
    )
    async def reservoir_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[add_reagent_method])

    @orca.thread(
        labware=neighbour,
        start=(NEIGHBOUR_SITE, REUSE_EXISTING),
        end=(NEIGHBOUR_SITE, LEAVE_IN_PLACE),
    )
    async def neighbour_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[add_reagent_method])

    @orca.workflow(name="resident_reagent_wf")
    def workflow(wf):
        wf.start(plate_journey)
        wf.thread(reservoir_journey)
        wf.thread(neighbour_journey)

    topology = Topology(
        locations={"stacker": stacker, "pad": pad, "lh": lh, "waste": waste},
        transporters=[arm],
    )
    build = await orca.build_system(
        name="Discharge Worlds", workflow=workflow, topology=topology, stores=stores)
    return build, lh


async def _deck_names(driver: RecordingLiquidHandlerDriver) -> set[str]:
    state = await driver.get_deck_state(GetDeckStateRequest())
    return {item.name for item in state.labware}


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_discharging_a_plate_a_pure_sim_run_left_behind_clears_that_deck() -> None:
    live = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    sim = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, _ = await _build(live, sim)
    runtime = SystemRuntime(
        build.system, event_bus=build.event_bus, labware_store=InMemoryLabwareStore(),
    )
    await runtime.start()
    try:
        record = await runtime.submit_workflow(
            "resident_reagent_wf", mode=WorkflowRunMode.PURE_SIM)
        await run_to_quiescence(runtime, record.id)

        on_sim_deck = await _deck_names(sim)
        resident = [n for n in on_sim_deck if named_for_template(n, "reservoir")]
        assert resident, (
            f"the run never left a reservoir on the sim deck; deck={on_sim_deck}"
        )

        rows = [
            row for row in await runtime.labware.list_all()
            if row.name in set(resident)
        ]
        assert rows, "the reservoir on the deck is not in the ledger"
        for row in rows:
            await runtime.labware.discharge_labware(row.id, force=True)

        left_behind = {
            n for n in await _deck_names(sim) if named_for_template(n, "reservoir")
        }
        assert not left_behind, (
            f"discharged {sorted(r.name for r in rows)} but the sim deck still "
            f"holds {sorted(left_behind)}; the next run is refused at that slot "
            f"by labware the operator was told is gone"
        )
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_the_discharge_leaves_the_plate_beside_it_on_that_same_deck() -> None:
    """Reaching further must not reach wider. The retract names one labware, so
    the resident standing next to it stays on the driver deck. Non-regression:
    it holds either side of the world fix, and would break the day a discharge
    goes back to rebuilding the whole deck."""
    live = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    sim = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, _ = await _build(live, sim)
    runtime = SystemRuntime(
        build.system, event_bus=build.event_bus, labware_store=InMemoryLabwareStore(),
    )
    await runtime.start()
    try:
        record = await runtime.submit_workflow(
            "resident_reagent_wf", mode=WorkflowRunMode.PURE_SIM)
        await run_to_quiescence(runtime, record.id)

        before = await _deck_names(sim)
        going = [n for n in before if named_for_template(n, "reservoir")]
        staying = [n for n in before if named_for_template(n, "neighbour")]
        assert going and staying, f"the run left {before} on the sim deck"

        for row in await runtime.labware.list_all():
            if row.name in set(going):
                await runtime.labware.discharge_labware(row.id, force=True)

        after = await _deck_names(sim)
        assert set(staying) <= after, (
            f"discharging {going} also took {sorted(set(staying) - after)} off "
            f"the deck; deck now holds {after}"
        )
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_clear_all_empties_the_deck_a_pure_sim_run_filled() -> None:
    """The panic button leaves nothing the ledger knew about standing on the
    deck. Non-regression: clear-all wipes every labware through the same
    `_clear_holders_at`, so it is the verb the world fix could most easily
    break, and it is the one an operator reaches for when the deck is wrong."""
    live = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    sim = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, _ = await _build(live, sim)
    runtime = SystemRuntime(
        build.system, event_bus=build.event_bus, labware_store=InMemoryLabwareStore(),
    )
    await runtime.start()
    try:
        record = await runtime.submit_workflow(
            "resident_reagent_wf", mode=WorkflowRunMode.PURE_SIM)
        await run_to_quiescence(runtime, record.id)

        residents = {
            n for n in await _deck_names(sim)
            if named_for_template(n, "reservoir") or named_for_template(n, "neighbour")
        }
        assert residents, "the run left nothing on the sim deck to clear"

        await runtime.labware.clear_all_labware(force=True)

        left_behind = residents & await _deck_names(sim)
        assert not left_behind, (
            f"clear-all left {sorted(left_behind)} on the sim deck"
        )
    finally:
        await runtime.shutdown()


# -- The bench shape: a run that finished, then a discharge ------------------
#
# Every thread above ends LEAVE_IN_PLACE mid-workflow, so the slot still holds
# the labware at discharge time. A standalone method run ends its threads for
# real: each one retires its labware and frees the slot, and the discharge that
# follows has no location left to match on.

_ROWS = "ABCDEFGH"

FLEX_DECK = DeckLayoutConfig(deck_type="FlexDeck", resources=[])

SOURCE_PAD = "pad_1"
FINAL_PAD = "incubator_1"
RACK_SLOT = "flex_1/B2-slot"


async def _build_finished_run(
    live: RecordingLiquidHandlerDriver, sim: RecordingLiquidHandlerDriver,
):
    """The bench's own workflow: a transfer whose tip rack lives on the deck."""
    stores = InMemoryRuntimeStoreFactory()
    source = PlateTemplate("r7_source", labware_type="Cor_96_wellplate_360ul_Fb")
    final = PlateTemplate("r7_final", labware_type="Cor_96_wellplate_360ul_Fb")
    rack = TipRackTemplate(
        "r7_tips", labware_type="flex_96_tiprack_1000ul", with_tips=True)

    with use_device_factory(_SplitLhFactory(live, sim)):
        flex = LiquidHandler(
            "flex_1",
            deck_layout_store=stores.deck_layouts("flex_1", seed={"default": FLEX_DECK}),
            deck_layout="default",
        )
        source_pad = PlatePad(SOURCE_PAD)
        final_pad = PlatePad(FINAL_PAD)
        arm = Transporter(
            "pf400_1",
            teachpoint_store=stores.teachpoints("pf400_1", seed=[
                Teachpoint(SOURCE_PAD, C(0, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint(FINAL_PAD, C(100, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("flex_1/C1-slot", C(200, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("flex_1/C2-slot", C(300, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint(RACK_SLOT, C(400, 200, 300, 0, 90, 180), orientation="right"),
            ]),
        )

    @orca.action(
        device=flex,
        inputs=[source, final, rack],
        deck_positions={source: "C2-slot", final: "C1-slot", rack: "B2-slot"},
    )
    async def transfer(ctx: ActionContext) -> None:
        lh = ctx.device(ILiquidHandler)
        from_plate = ctx.plate("r7_source")
        to_plate = ctx.plate("r7_final")
        await lh.pick_up_tips(await ctx.next_tips("r7_tips", len(_ROWS)))
        await lh.aspirate(
            [from_plate.well(f"{r}1") for r in _ROWS], [10.0] * 8, flow_rates=[100.0] * 8)
        await lh.dispense(
            [to_plate.well(f"{r}1") for r in _ROWS], [10.0] * 8, flow_rates=[100.0] * 8)
        await lh.discard_tips()

    @orca.method
    async def transfer_method(ctx: MethodContext):
        yield transfer

    @orca.thread(
        labware=source, start=SOURCE_PAD, end=SOURCE_PAD, contributes_to=["r7_final"])
    async def source_journey(ctx: ThreadContext):
        yield transfer_method

    @orca.thread(labware=final, start=FINAL_PAD, end=FINAL_PAD)
    async def final_receiver(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[transfer_method])

    @orca.thread(
        labware=rack,
        start=(RACK_SLOT, REUSE_EXISTING),
        end=(RACK_SLOT, LEAVE_IN_PLACE),
    )
    async def tips_supply(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[transfer_method])

    @orca.workflow(name="transfer_wf")
    def workflow(wf):
        wf.start(source_journey)
        wf.thread(final_receiver)
        wf.thread(tips_supply)

    topology = Topology(
        locations={"flex_1": flex, SOURCE_PAD: source_pad, FINAL_PAD: final_pad},
        transporters=[arm],
    )
    build = await orca.build_system(
        name="Bench Shape", workflow=workflow, topology=topology, stores=stores)
    return build


_STARTS = {"r7_source": SOURCE_PAD, "r7_final": FINAL_PAD, "r7_tips": RACK_SLOT}


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_a_finished_run_leaves_nothing_a_discharge_cannot_reach() -> None:
    """The bench, end to end. Run a standalone method, discharge everything it
    left, run it again. The second run used to be refused its deck slot by the
    rack the operator had just discharged, and only a restart cleared it.

    Two things keep this green now: the departure a finishing thread projects,
    and the discharge's own retract. It guards the outcome rather than either
    mechanism, so it stays honest if one of them moves."""
    live = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    sim = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build = await _build_finished_run(live, sim)
    runtime = SystemRuntime(
        build.system, event_bus=build.event_bus, labware_store=InMemoryLabwareStore(),
    )
    await runtime.start()
    try:
        first = await runtime.submit_method(
            "transfer_wf", "transfer_method", labware_start=_STARTS,
            labware_end=_STARTS, mode=WorkflowRunMode.PURE_SIM)
        statuses = await run_to_quiescence(runtime, first.id)
        assert set(statuses.values()) == {"COMPLETED"}, f"first run: {statuses}"

        rows = await runtime.labware.list_all()
        assert rows, "the run recorded no labware to discharge"
        for row in rows:
            await runtime.labware.discharge_labware(row.id, force=True)
        assert not await runtime.labware.list_all(), "the ledger still holds rows"

        left_behind = {
            n for n in await _deck_names(sim) if named_for_template(n, "r7_tips")
        }
        assert not left_behind, (
            f"the discharged rack {sorted(left_behind)} is still on the driver "
            f"deck, so the next run is refused its slot"
        )

        second = await runtime.submit_method(
            "transfer_wf", "transfer_method", labware_start=_STARTS,
            labware_end=_STARTS, mode=WorkflowRunMode.PURE_SIM)
        statuses = await run_to_quiescence(runtime, second.id)
        assert set(statuses.values()) == {"COMPLETED"}, (
            f"the run after the discharge did not complete: {statuses}"
        )
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_moving_a_plate_off_a_deck_clears_the_world_it_was_really_in() -> None:
    """An operator stating a new position writes it in their own world. The
    worlds they are not in were left holding the plate at the site it had left,
    which is the same refusal a discharge used to cause, one verb over."""
    live = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    sim = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, _ = await _build(live, sim)
    runtime = SystemRuntime(
        build.system, event_bus=build.event_bus, labware_store=InMemoryLabwareStore(),
    )
    await runtime.start()
    try:
        record = await runtime.submit_workflow(
            "resident_reagent_wf", mode=WorkflowRunMode.PURE_SIM)
        await run_to_quiescence(runtime, record.id)

        resident = [
            row for row in await runtime.labware.list_all()
            if named_for_template(row.name, "reservoir")
        ]
        assert resident, "the run left no reservoir to move"
        moved = resident[0]
        assert moved.name in await _deck_names(sim)

        await runtime.labware.edit_location(
            moved.id, "pad", reason="carried it to the pad", confirm=True)

        assert moved.name not in await _deck_names(sim), (
            f"{moved.name} is on the pad now, but the sim deck still stands it "
            f"at {RESERVOIR_SITE}, which refuses the next run that site"
        )
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_clear_all_reaches_a_resident_no_orca_record_knows_about() -> None:
    """The panic button's whole job. A plate loaded through the vendor's own
    app, or left by an earlier process, is in a driver world and in no orca
    record, so only a deck-wide reset per world can reach it."""
    live = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    sim = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, lh = await _build(live, sim)
    runtime = SystemRuntime(
        build.system, event_bus=build.event_bus, labware_store=InMemoryLabwareStore(),
    )
    await runtime.start()
    try:
        # Lay both worlds out, then put a plate on the live one behind orca's
        # back, the way a vendor app does.
        for mode in (WorkflowRunMode.PURE_SIM, WorkflowRunMode.LIVE):
            with mode_scope(mode):
                assert await lh.deck_world_layout() is not None
        await live.add_deck_labware(AddDeckLabwareRequest(
            name="stranger", catalog_ref="Cor_Falcon_96_wellplate_340ul_Fb_Black",
            at="carrier-7-0", well_state=None,
        ))
        assert "stranger" in await _deck_names(live)

        await runtime.labware.clear_all_labware(force=True)

        assert "stranger" not in await _deck_names(live), (
            "clear-all left a plate on the live deck because it only reset the "
            "world the caller happened to be in"
        )
    finally:
        await runtime.shutdown()
