"""A LEAVE_IN_PLACE deck resident must serve the next execution too.

`REUSE_EXISTING` + `LEAVE_IN_PLACE` is the declared pattern for deck-resident
consumables (tip racks, reagent troughs): the labware stays on the deck when a
run ends and the next run binds it without re-registration. The labware keeps
its identity across runs on purpose -- the thread factory appends a fresh
per-execution ops bucket to it -- so a second run asks the thread registry for
a thread id the first run already retired.

Found on hardware 2026-08-27: the second submission died on
"Cannot fire CO_LABWARE_AWAITED from COMPLETED" and stranded a plate on an
incubator, because the second run was handed the first run's terminal thread.
"""

import orca.orca as orca
from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig, Teachpoint
from cheshire_drivers import CartesianCoordinates as C
from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.submission import BatchMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, REUSE_EXISTING
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from tests.test_helpers import run_to_quiescence


_DECK_CONFIG = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        DeckResourceConfig(name="carrier-25", catalog_ref="Trough_CAR_4R200_A00", rail=25),
    ],
)
_RESERVOIR_SITE = "lh/carrier-25-0"
_ARM_DECK_ENTRY = "lh/carrier-7-2"


async def _build_resident_system():
    stores = InMemoryRuntimeStoreFactory()
    sample = PlateTemplate("sample", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    reservoir = PlateTemplate("reservoir", labware_type="AGenBio_1_troughplate_190000uL_Fl")

    lh = LiquidHandler(
        "lh",
        deck_layout_store=stores.deck_layouts("lh", seed={"default": _DECK_CONFIG}),
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
            Teachpoint(_ARM_DECK_ENTRY, C(400, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint("waste", C(600, 200, 300, 0, 90, 180), orientation="right"),
        ]),
    )

    @orca.action(device=lh, inputs=[sample, reservoir], deck_positions={sample: "carrier-7-0"})
    async def add_reagent(ctx: ActionContext) -> None:
        ctx.labware("reservoir")

    @orca.method
    async def add(ctx: MethodContext):
        yield add_reagent

    @orca.thread(labware=sample, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield add

    @orca.thread(
        labware=reservoir,
        start=(_RESERVOIR_SITE, REUSE_EXISTING),
        end=(_RESERVOIR_SITE, LEAVE_IN_PLACE),
    )
    async def reservoir_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[add])

    @orca.workflow(name="resident_wf")
    def workflow(wf):
        wf.start(plate_journey)
        wf.thread(reservoir_journey)

    topology = Topology(
        locations={"stacker": stacker, "lh": lh, "pad": pad, "waste": waste},
        transporters=[arm],
    )
    return await orca.build_system(
        name="Deck Resident", workflow=workflow, topology=topology, stores=stores)


def _thread_name(statuses: dict[str, str], template_name: str) -> str:
    matches = [name for name in statuses if name.startswith(f"{template_name}-")]
    assert len(matches) == 1, f"expected one {template_name} thread, got {matches}"
    return matches[0]


async def test_a_left_in_place_resident_serves_the_next_execution() -> None:
    build = await _build_resident_system()
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    template = build.system.get_workflow_template("resident_wf")
    await runtime.start()
    try:
        first = await runtime.submit(template, mode=WorkflowRunMode.PURE_SIM)
        first_statuses = await run_to_quiescence(runtime, first.execution_id, timeout=60.0)
        assert first_statuses and all(s == "COMPLETED" for s in first_statuses.values()), (
            f"first run must complete; got {first_statuses}"
        )

        second = await runtime.submit(template, mode=WorkflowRunMode.PURE_SIM)
        second_statuses = await run_to_quiescence(runtime, second.execution_id, timeout=60.0)
    finally:
        await runtime.shutdown(confirm=True)

    assert second_statuses and all(s == "COMPLETED" for s in second_statuses.values()), (
        f"the resident left in place by run 1 must serve run 2; got {second_statuses}"
    )
    assert _thread_name(second_statuses, "reservoir") == _thread_name(first_statuses, "reservoir"), (
        "the resident keeps one identity across runs: the rack never left the deck"
    )
    assert _thread_name(second_statuses, "sample") != _thread_name(first_statuses, "sample"), (
        "each run dispenses its own plate, so transit labware gets a new identity"
    )


async def test_a_joining_submission_is_served_by_the_same_resident() -> None:
    """A JOIN_EXISTING submission delivered into a run already in flight is
    served by the resident already on the deck -- one trough, one identity, no
    second registration."""
    build = await _build_resident_system()
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    template = build.system.get_workflow_template("resident_wf")
    await runtime.start()
    try:
        first = await runtime.submit(
            template,
            groups=[LabwareGroup(id="g1", members=(
                LabwareGroupMember(thread_template_name="plate_journey"),))],
            mode=WorkflowRunMode.PURE_SIM,
        )
        second = await runtime.submit(
            template,
            groups=[LabwareGroup(id="g2", members=(
                LabwareGroupMember(thread_template_name="plate_journey"),))],
            batch_mode=BatchMode.JOIN_EXISTING,
            mode=WorkflowRunMode.PURE_SIM,
        )
        assert second.execution_id == first.execution_id, (
            "JOIN_EXISTING must deliver into the in-flight execution"
        )
        statuses = await run_to_quiescence(runtime, second.execution_id, timeout=60.0)
    finally:
        await runtime.shutdown(confirm=True)

    assert statuses and all(s == "COMPLETED" for s in statuses.values()), (
        f"both plates and the resident must complete; got {statuses}"
    )
    plates = [name for name in statuses if name.startswith("sample-")]
    assert len(plates) == 2, f"each submission brings its own plate; got {plates}"
    reservoirs = [name for name in statuses if name.startswith("reservoir-")]
    assert len(reservoirs) == 1, (
        f"both submissions draw on the one trough on the deck; got {reservoirs}"
    )
