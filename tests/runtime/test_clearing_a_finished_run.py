"""Clearing the platform after a run: the run's plates go, the resident stays.

At the end of a run the operator wants the deck back without losing the
reagents and tip racks that live on it. `clear_submission_labware` is the verb
for that: it wipes the submission's transit labware and skips anything a thread
declared `LEAVE_IN_PLACE`, because a deck resident's identity is what carries
its consumed volume and remaining tips into the next run.

The run being finished is the normal case, not an edge one -- nobody clears a
deck mid-run -- so the walk has to reach a completed execution's threads.
"""

import pytest

import orca.orca as orca
from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig, Teachpoint
from cheshire_drivers import CartesianCoordinates as C
from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, REUSE_EXISTING
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.runtime.execution import ExecutionPhase
from tests.test_helpers import execution_outcome


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

    @orca.thread(labware=sample, start=("stacker", DISPENSE), end="pad")
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


def _ids_by_template(snapshots) -> dict[str, str]:
    return {s.template_name: s.id for s in snapshots}


async def test_clearing_a_finished_run_takes_its_plates_and_leaves_the_resident() -> None:
    build = await _build_resident_system()
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    template = build.system.get_workflow_template("resident_wf")
    await runtime.start()
    try:
        submission = await runtime.submit(template, mode=WorkflowRunMode.PURE_SIM)
        outcome = await execution_outcome(runtime, submission, timeout=60.0)
        assert outcome.status is ExecutionPhase.COMPLETED, (
            f"the run must finish before the deck is cleared; got {outcome.status}"
        )
        before = _ids_by_template(await runtime.labware.list_all())
        assert {"sample", "reservoir"} <= before.keys(), (
            f"both labware must be on the system after the run; got {before}"
        )

        result = await runtime.labware.clear_submission_labware(
            submission.id, force=False,
        )

        after = _ids_by_template(await runtime.labware.list_all())
    finally:
        await runtime.shutdown(confirm=True)

    assert result.cleared == [before["sample"]], (
        f"the plate the run consumed comes off the platform; got {result.cleared}"
    )
    assert result.preserved_reuse_bound == [before["reservoir"]], (
        "the operator has to be told which reagents were left on the deck; "
        f"got {result.preserved_reuse_bound}"
    )
    assert "sample" not in after, "the cleared plate must be gone from the world model"
    assert after.get("reservoir") == before["reservoir"], (
        "the resident keeps its identity, which is what carries its volume "
        "into the next run"
    )


async def test_a_submission_the_runtime_never_saw_is_an_error_not_an_empty_clear() -> None:
    """An id that matches nothing clears nothing, and an operator reads that as
    "the deck is already clear" -- the opposite of what a stale or mistyped id
    means."""
    build = await _build_resident_system()
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        with pytest.raises(KeyError):
            await runtime.labware.clear_submission_labware("no-such-submission")
    finally:
        await runtime.shutdown(confirm=True)
