"""A reuse-resident reagent pinned to a deck site must be usable as real
liquid-handler labware: seeded into the driver deck (so pipetting from it
works) and bound exactly once (reused, never re-spawned).

These are the TDD reds for the PR-D follow-up flagged on the deck-site work:
a resident reagent is placed orca-side on its DeckSite but never registered
with the PLR driver (only transit labware is, via move_plate on arrival), so
`aspirate(reagent.well(...))` raised `Resource '<reagent>' not found`.
"""

import pytest
from pydantic import JsonValue

import orca.orca as orca
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, REUSE_EXISTING
from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig, Teachpoint
from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from cheshire_drivers.liquid_handler_models import GetDeckStateRequest
from orca.devices.device_interfaces import ILiquidHandler
from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory_protocol import DriverPairElement
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate, TipRackTemplate
from orca.state.records import LabwareInitialState
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from tests.test_helpers import run_to_quiescence, named_for_template


DECK_CONFIG = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        DeckResourceConfig(name="carrier-25", catalog_ref="Trough_CAR_4R200_A00", rail=25),
    ],
)

RESERVOIR_SITE = "lh/carrier-25-0"
# The arm reaches exactly this deck site; the gripper relays onward from it.
ARM_DECK_ENTRY = "lh/carrier-7-2"


async def _build(recorder: RecordingLiquidHandlerDriver, joins: int):
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

    @orca.action(
        device=lh,
        inputs=[sample_plate, reservoir],
        deck_positions={sample_plate: "carrier-7-0"},
    )
    async def add_reagent(ctx: ActionContext) -> None:
        ctx.labware("reservoir")

    @orca.method
    async def add_reagent_method(ctx: MethodContext):
        yield add_reagent

    @orca.thread(labware=sample_plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        for _ in range(joins):
            yield add_reagent_method

    @orca.thread(
        labware=reservoir,
        start=(RESERVOIR_SITE, REUSE_EXISTING),
        end=(RESERVOIR_SITE, LEAVE_IN_PLACE),
    )
    async def reservoir_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[add_reagent_method])

    @orca.workflow(name="resident_reagent_wf")
    def workflow(wf):
        wf.start(plate_journey)
        wf.thread(reservoir_journey)

    topology = Topology(
        locations={"stacker": stacker, "pad": pad, "lh": lh, "waste": waste},
        transporters=[arm],
    )
    build = await orca.build_system(
        name="Resident Reagent", workflow=workflow, topology=topology, stores=stores)
    return build, lh


@pytest.mark.slow
@pytest.mark.asyncio
async def test_resident_reagent_is_seeded_into_the_plr_deck() -> None:
    """After a resident reagent binds to its deck site, the driver deck knows
    about it (so liquid ops can address it). RED today: it is never seeded."""
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, lh = await _build(recorder, joins=1)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    record = await runtime.submit_workflow("resident_reagent_wf", mode=WorkflowRunMode.PURE_SIM)
    await run_to_quiescence(runtime, record.id)

    deck = await lh.driver.get_deck_state(GetDeckStateRequest())
    names = {item.name for item in deck.labware}
    assert any(named_for_template(n, "reservoir") for n in names), (
        f"resident reagent not registered on the PLR deck; deck labware={names}"
    )


@pytest.mark.slow
@pytest.mark.asyncio
async def test_resident_reagent_is_reused_not_respawned() -> None:
    """Two methods both consume the resident reagent; it must bind ONCE (same
    instance), not spawn a fresh reagent per join."""
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, _ = await _build(recorder, joins=2)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    record = await runtime.submit_workflow("resident_reagent_wf", mode=WorkflowRunMode.PURE_SIM)
    await run_to_quiescence(runtime, record.id)

    reservoir_threads = [
        t for t in runtime.list_threads(record.id) if t.name.startswith("reservoir")
    ]
    assert len(reservoir_threads) == 1, (
        f"expected exactly one resident reagent thread (reused), got "
        f"{[t.name for t in reservoir_threads]}"
    )


DECK_CONFIG_PIPETTE = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        DeckResourceConfig(name="carrier-25", catalog_ref="Trough_CAR_4R200_A00", rail=25),
        DeckResourceConfig(name="carrier-15", catalog_ref="TIP_CAR_480_A00", rail=15),
    ],
)


async def build_pipetting_bench(
    recorder: RecordingLiquidHandlerDriver,
    *,
    transfer: bool = False,
    reservoir_volume: float | None = None,
):
    """``reservoir_volume`` declares what the reservoir starts with, which is
    what gives it a contents baseline: without one it reads UNKNOWN forever and
    a test asserting anything else about its provenance asserts nothing."""
    stores = InMemoryRuntimeStoreFactory()

    sample_plate = PlateTemplate(
        "sample_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    reservoir = PlateTemplate(
        "reservoir", labware_type="AGenBio_1_troughplate_190000uL_Fl",
        initial_state=(
            LabwareInitialState(uniform_volume=reservoir_volume)
            if reservoir_volume is not None else None
        ),
    )
    dest_reservoir = PlateTemplate(
        "dest_reservoir", labware_type="AGenBio_1_troughplate_190000uL_Fl")
    tips = TipRackTemplate(
        "tips", labware_type="hamilton_96_tiprack_10uL_filter", with_tips=True)

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
            deck_layout_store=stores.deck_layouts("lh", seed={"default": DECK_CONFIG_PIPETTE}),
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

    action_inputs = [sample_plate, reservoir, tips] + (
        [dest_reservoir] if transfer else [])

    @orca.action(
        device=lh,
        inputs=action_inputs,
        deck_positions={sample_plate: "carrier-7-0"},
    )
    async def pipette_from_reservoir(ctx: ActionContext) -> None:
        handler = ctx.device(ILiquidHandler)
        rack = ctx.tip_rack("tips")
        source = ctx.plate("reservoir").well("A1")
        dest = ctx.plate("dest_reservoir").well("A1") if transfer else source
        await handler.pick_up_tips([rack.tip_spot("A1")])
        await handler.aspirate([source], [10.0])
        await handler.dispense([dest], [10.0])
        await handler.drop_tips([rack.tip_spot("A1")])

    @orca.method
    async def pipette_method(ctx: MethodContext):
        yield pipette_from_reservoir

    @orca.thread(labware=sample_plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield pipette_method

    @orca.thread(
        labware=reservoir,
        start=("lh/carrier-25-0", REUSE_EXISTING),
        end=("lh/carrier-25-0", LEAVE_IN_PLACE),
    )
    async def reservoir_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[pipette_method])

    @orca.thread(
        labware=tips,
        start=("lh/carrier-15-0", REUSE_EXISTING),
        end=("lh/carrier-15-0", LEAVE_IN_PLACE),
    )
    async def tips_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[pipette_method])

    if transfer:
        @orca.thread(
            labware=dest_reservoir,
            start=("lh/carrier-25-1", REUSE_EXISTING),
            end=("lh/carrier-25-1", LEAVE_IN_PLACE),
        )
        async def dest_reservoir_journey(ctx: ThreadContext):
            while ctx.has_more_work():
                yield orca.join(allows=[pipette_method])

    @orca.workflow(name="pipetting_wf")
    def workflow(wf):
        wf.start(plate_journey)
        wf.thread(reservoir_journey)
        wf.thread(tips_journey)
        if transfer:
            wf.thread(dest_reservoir_journey)

    topology = Topology(
        locations={"stacker": stacker, "pad": pad, "lh": lh, "waste": waste},
        transporters=[arm],
    )
    build = await orca.build_system(
        name="Resident Pipetting", workflow=workflow, topology=topology, stores=stores)
    return build, lh


@pytest.mark.slow
@pytest.mark.asyncio
async def test_pipette_from_resident_reagent_completes() -> None:
    """An action aspirates from a reuse-resident reagent and picks tips from a
    resident rack. Before the seeding fix this raised Resource-not-found and the
    thread errored; now the driver deck holds both, so the workflow completes and
    the aspirate reaches the driver."""
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, _ = await build_pipetting_bench(recorder)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    record = await runtime.submit_workflow("pipetting_wf", mode=WorkflowRunMode.PURE_SIM)
    statuses = await run_to_quiescence(runtime, record.id)

    assert statuses, "no threads were created"
    assert all(s == "COMPLETED" for s in statuses.values()), (
        f"every thread must reach COMPLETED -- the owner pipettes from the "
        f"resident reagent and the resident reservoir/tips receivers terminate "
        f"on quiescence: {statuses}"
    )
    aspirates = [c for c in recorder.calls if c.method == "aspirate"]
    assert aspirates, "no aspirate reached the driver"


def _single_target(
    recorder: RecordingLiquidHandlerDriver, method: str,
) -> dict[str, JsonValue]:
    """The one labware target of the single recorded aspirate/dispense call."""
    key = "aspirations" if method == "aspirate" else "dispenses"
    calls = [c for c in recorder.calls if c.method == method]
    assert len(calls) == 1, f"expected exactly one {method}, got {len(calls)}"
    targets = calls[0].args[key]
    assert len(targets) == 1, f"expected one labware target, got {targets}"
    return targets[0]


@pytest.mark.slow
@pytest.mark.asyncio
async def test_pipette_round_trip_issues_correct_commands() -> None:
    """Command-level verification: the round-trip issues exactly one aspirate and
    one dispense, both addressing reservoir well A1 at 10uL. Volume introspection
    via IWell is not possible here -- the driver mutates its own PLR well, a
    different object than ctx.plate() exposes (see docs/plr-two-entrances.md) --
    so the driver command is the reachable proof the pipetting was issued."""
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, _ = await build_pipetting_bench(recorder)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    record = await runtime.submit_workflow("pipetting_wf", mode=WorkflowRunMode.PURE_SIM)
    statuses = await run_to_quiescence(runtime, record.id)
    assert all(s == "COMPLETED" for s in statuses.values()), statuses

    reservoir_name = next(
        lw.name for lw in build.system.labwares if lw.template_name == "reservoir")
    assert _single_target(recorder, "aspirate") == {
        "labware": reservoir_name, "positions": ["A1"], "volumes": [10.0]}
    assert _single_target(recorder, "dispense") == {
        "labware": reservoir_name, "positions": ["A1"], "volumes": [10.0]}


@pytest.mark.slow
@pytest.mark.asyncio
async def test_pipette_transfers_between_resident_reagents() -> None:
    """Command-level verification of a real transfer: aspirate 10uL from the
    reservoir's well A1 and dispense into a SECOND resident reservoir's well A1.
    Proves the transfer is addressed to two distinct on-deck residents (both
    seeded into the driver deck), not a round-trip into one well."""
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, _ = await build_pipetting_bench(recorder, transfer=True)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    record = await runtime.submit_workflow("pipetting_wf", mode=WorkflowRunMode.PURE_SIM)
    statuses = await run_to_quiescence(runtime, record.id)
    assert all(s == "COMPLETED" for s in statuses.values()), statuses

    by_template = {lw.template_name: lw.name for lw in build.system.labwares}
    assert _single_target(recorder, "aspirate") == {
        "labware": by_template["reservoir"], "positions": ["A1"], "volumes": [10.0]}
    assert _single_target(recorder, "dispense") == {
        "labware": by_template["dest_reservoir"], "positions": ["A1"], "volumes": [10.0]}


@pytest.mark.slow
@pytest.mark.asyncio
async def test_resident_reagent_volume_carries_into_the_next_run() -> None:
    """Two runs against one deck. The trough keeps its identity across them, so
    the volume it has given up accumulates: run 2 reads a trough already down by
    run 1's draw, not a full one. This is why a deck resident must not be minted
    fresh per run -- a new id each run would report a half-used trough as full,
    and a half-used tip rack as untouched."""
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, _ = await build_pipetting_bench(recorder, transfer=True)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()

    def _reservoir_id() -> str:
        return next(
            lw.id for lw in build.system.labwares if lw.template_name == "reservoir")

    first = await runtime.submit_workflow("pipetting_wf", mode=WorkflowRunMode.PURE_SIM)
    statuses = await run_to_quiescence(runtime, first.id)
    assert all(s == "COMPLETED" for s in statuses.values()), statuses
    reservoir_id = _reservoir_id()
    after_first = (await runtime.labware.get_well_volumes(reservoir_id)).volumes

    second = await runtime.submit_workflow("pipetting_wf", mode=WorkflowRunMode.PURE_SIM)
    statuses = await run_to_quiescence(runtime, second.id)
    assert all(s == "COMPLETED" for s in statuses.values()), statuses
    assert _reservoir_id() == reservoir_id, (
        "the trough never left the deck, so it is still the same trough"
    )
    after_second = (await runtime.labware.get_well_volumes(reservoir_id)).volumes

    assert after_second["A1"] == after_first["A1"] - 10.0, (
        f"run 2's 10uL draw must come off what run 1 left: "
        f"{after_first['A1']} then {after_second['A1']}"
    )
