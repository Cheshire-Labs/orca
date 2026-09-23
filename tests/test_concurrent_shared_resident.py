"""Two INSTANCES of one plate template (multi-group) drawing from a shared resident
reagent must both complete.

Isolates the multi-plate SMC failure to its smallest form. Static concurrent
threads sharing a resident or a deck site already serialize cleanly (proven); the
open question is whether spawning the SAME template as N group instances collides
on its fixed deck site / the shared resident's method stream.

RED reproduces the SMC batch deadlock (deck double-claim / resident re-entrancy);
GREEN once the multi-plate spawn path serializes plate entry onto the handler.
"""

import pytest

import orca.orca as orca
from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig, Teachpoint
from cheshire_drivers import CartesianCoordinates as C
from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.run_modes import WorkflowRunMode
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
# The arm reaches exactly this deck site; the on-deck gripper relays from it
# to the sample's working site.
_ARM_DECK_ENTRY = "lh/carrier-7-2"


async def _build_one_template():
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

    @orca.workflow(name="shared_resident_wf")
    def workflow(wf):
        wf.start(plate_journey)
        wf.thread(reservoir_journey)

    topology = Topology(
        locations={"stacker": stacker, "lh": lh, "pad": pad, "waste": waste},
        transporters=[arm],
    )
    return await orca.build_system(
        name="Shared Resident", workflow=workflow, topology=topology, stores=stores)


@pytest.mark.slow
@pytest.mark.timeout(120)
@pytest.mark.asyncio
async def test_two_group_instances_share_one_resident_reagent() -> None:
    build = await _build_one_template()
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    template = build.system.get_workflow_template("shared_resident_wf")
    await runtime.start()
    groups = [
        LabwareGroup(id=f"grp-{i}", members=(LabwareGroupMember(thread_template_name="plate_journey"),))
        for i in range(2)
    ]
    submission = await runtime.submit(template, groups=groups, mode=WorkflowRunMode.PURE_SIM)

    statuses = await run_to_quiescence(runtime, submission.execution_id, timeout=60.0)
    await runtime.shutdown(confirm=True)

    assert statuses, "no threads reported"
    assert all(s == "COMPLETED" for s in statuses.values()), (
        f"both plate instances and the shared resident must complete; got {statuses}"
    )
