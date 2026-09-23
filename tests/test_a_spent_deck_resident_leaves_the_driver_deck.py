"""A used-up deck resident has to leave the DRIVER's deck, not just its site.

`REUSE_EXISTING` + `LEAVE_IN_PLACE` on a liquid-handler deck site is how a
deck-resident consumable is declared, and it is the shape the bench ran:
`start=("flex_1/B2-slot", REUSE_EXISTING)`.

A receiver that ends spent is disposed instead of left in place, and
`DeckSite.dispose_labware` clears the site's occupancy and tells the driver
nothing. Without a matching projection the driver deck still holds the spent
labware materialized, and the replacement receiver's own projection lands on
an occupied slot: `ValueError: spot 0 already has a resource`, receiver
FAILED, slot orphaned. A hang turned into a hard failure on exactly the
topology the fix was written for.

The sibling cases (plain pads) live in
tests/test_a_depleted_receiver_hands_off.py; this file is the deck-site one
because only a real deck has a driver world to disagree with.
"""

import orca.orca as orca
from cheshire_drivers import CartesianCoordinates as C
from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig, Teachpoint
from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
from orca.resource_models.transporter import Transporter
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import SystemBuild, Topology
from orca.sdk.labware import PlateTemplate
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, REUSE_EXISTING
from orca.state.records import DeclaredTracking
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


class _OneDrawEach:
    """A reservoir that answers can_continue False after a single draw."""

    def __init__(self) -> None:
        self._draws: dict[str, int] = {}
        self.served_by: list[str] = []

    def draw(self, labware: LabwareInstance) -> None:
        self._draws[labware.id] = self._draws.get(labware.id, 0) + 1
        self.served_by.append(labware.id)

    async def can_continue(
        self, labware: LabwareInstance, demand: DeclaredTracking | None = None,
    ) -> bool:
        del demand
        return self._draws.get(labware.id, 0) < 1


async def _build_system(depletion: _OneDrawEach) -> SystemBuild:
    stores = InMemoryRuntimeStoreFactory()
    sample = PlateTemplate(
        "sample", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black",
    )
    reservoir = PlateTemplate(
        "reservoir",
        labware_type="AGenBio_1_troughplate_190000uL_Fl",
        can_continue_fn=depletion.can_continue,
        group_sharing=GroupSharing.SHARED_ACROSS_GROUPS,
        submission_batching=SubmissionBatching.BATCHABLE,
    )

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

    @orca.action(
        device=lh, inputs=[sample, reservoir], deck_positions={sample: "carrier-7-0"},
    )
    async def add_reagent(ctx: ActionContext) -> None:
        depletion.draw(ctx.labware("reservoir"))

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

    @orca.workflow(name="spent_resident_wf")
    def workflow(wf):
        wf.start(plate_journey)
        wf.thread(reservoir_journey)

    topology = Topology(
        locations={"stacker": stacker, "lh": lh, "pad": pad, "waste": waste},
        transporters=[arm],
    )
    return await orca.build_system(
        name="Spent Deck Resident", workflow=workflow, topology=topology, stores=stores,
    )


def _plate_group(gid: str) -> LabwareGroup:
    return LabwareGroup(
        id=gid, members=(LabwareGroupMember(thread_template_name="plate_journey"),),
    )


async def test_a_spent_deck_resident_is_replaced_without_colliding_on_its_slot() -> None:
    depletion = _OneDrawEach()
    build = await _build_system(depletion)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    template = build.system.get_workflow_template("spent_resident_wf")
    await runtime.start()
    try:
        submission = await runtime.submit(
            template,
            groups=[_plate_group("grp-1"), _plate_group("grp-2")],
            mode=WorkflowRunMode.PURE_SIM,
        )
        statuses = await run_to_quiescence(
            runtime, submission.execution_id, timeout=90.0,
        )
    finally:
        await runtime.shutdown()

    assert len(depletion.served_by) == 2, (
        f"both plates must get their reagent, got {depletion.served_by}"
    )
    assert depletion.served_by[0] != depletion.served_by[1], (
        "the second plate needs a FRESH reservoir; adopting the spent one "
        f"overflows again, got {depletion.served_by}"
    )
    assert statuses and all(s == "COMPLETED" for s in statuses.values()), (
        "the replacement's deck projection collided with a spent reservoir "
        f"the driver still holds; statuses={statuses}"
    )
