"""A thread that dies on an error nothing caught.

Every fault the lab can produce parks a thread for a recovery decision, so
reaching the crash path means the engine itself failed. It used to take the
whole run down with it: sibling threads were cancelled mid-journey and their
labware was left wherever the cancel landed, with nothing recorded to say
where. That is what a bench run hit, and the operator was left hunting for a
plate the system had stopped talking about.

A crash stops that one thread. The rest of the run finishes, and the operator
gets an incident naming the labware and the position it stopped at, which is
what clearing the deck by hand needs.
"""

import pytest

import orca.orca as orca
from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig, Teachpoint
from cheshire_drivers import CartesianCoordinates as C
from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.transporter import Transporter
from orca.runtime.incident_store import IncidentCategory
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, REUSE_EXISTING
from orca.system.reservation_manager.errors import ThreadDiedContext
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from tests.test_helpers import wait_for_runtime_condition


_DECK_CONFIG = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        DeckResourceConfig(name="carrier-25", catalog_ref="Trough_CAR_4R200_A00", rail=25),
    ],
)
_RESERVOIR_SITE = "lh/carrier-25-0"


async def _build_system():
    stores = InMemoryRuntimeStoreFactory()
    sample = PlateTemplate("sample", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    spare = PlateTemplate("spare", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    reservoir = PlateTemplate("reservoir", labware_type="AGenBio_1_troughplate_190000uL_Fl")

    lh = LiquidHandler(
        "lh",
        deck_layout_store=stores.deck_layouts("lh", seed={"default": _DECK_CONFIG}),
        deck_layout="default",
    )
    stacker = Storage("stacker")
    waste = Storage("waste")
    arm = Transporter(
        "arm",
        teachpoint_store=stores.teachpoints("arm", seed=[
            Teachpoint("stacker", C(0, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint("lh/carrier-7-2", C(400, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint("waste", C(600, 200, 300, 0, 90, 180), orientation="right"),
        ]),
    )

    @orca.action(device=lh, inputs=[sample, reservoir], deck_positions={sample: "carrier-7-0"})
    async def add_reagent(ctx: ActionContext) -> None:
        ctx.labware("reservoir")

    @orca.method
    async def add(ctx: MethodContext):
        yield add_reagent

    @orca.action(device=lh, inputs=[spare], deck_positions={spare: "carrier-7-0"})
    async def touch_spare(ctx: ActionContext) -> None:
        ctx.labware("spare")

    @orca.method
    async def add_nothing(ctx: MethodContext):
        yield touch_spare

    @orca.thread(labware=sample, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield add

    # Shares no method, no device and no labware with the plate: whatever
    # happens to it is nobody else's business.
    @orca.thread(labware=spare, start=("stacker", DISPENSE), end="waste")
    async def spare_journey(ctx: ThreadContext):
        yield add_nothing

    @orca.thread(
        labware=reservoir,
        start=(_RESERVOIR_SITE, REUSE_EXISTING),
        end=(_RESERVOIR_SITE, LEAVE_IN_PLACE),
    )
    async def reservoir_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[add])

    @orca.workflow(name="crash_wf")
    def workflow(wf):
        wf.start(plate_journey)
        wf.start(spare_journey)
        wf.thread(reservoir_journey)

    topology = Topology(
        locations={"stacker": stacker, "lh": lh, "waste": waste}, transporters=[arm])
    return await orca.build_system(
        name="Dead Thread", workflow=workflow, topology=topology, stores=stores)


@pytest.mark.timeout(90)
async def test_a_crashed_thread_records_its_labware_and_leaves_the_run_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = await _build_system()

    # An engine fault, injected where one actually escaped on the bench: outside
    # the thread's own error handling, so nothing pauses and nothing recovers.
    original = ExecutingLabwareThread.initialize_labware

    async def crash_the_reagent_thread(self: ExecutingLabwareThread) -> None:
        if self.thread_instance.labware.template_name == "reservoir":
            raise RuntimeError("simulated engine fault")
        await original(self)

    monkeypatch.setattr(
        ExecutingLabwareThread, "initialize_labware", crash_the_reagent_thread)

    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    template = build.system.get_workflow_template("crash_wf")
    await runtime.start()
    try:
        submission = await runtime.submit(template, mode=WorkflowRunMode.PURE_SIM)
        await wait_for_runtime_condition(
            runtime,
            lambda: any(
                t.name.startswith("reservoir-") and t.status == "FAILED"
                for t in runtime.list_threads(submission.execution_id)
            ),
            timeout=30.0,
            message="the crashed reagent thread never reached FAILED",
        )
        # Wait until the plate has settled, so a teardown would have reached it.
        settled = {"PAUSED", "COMPLETED", "ABORTED", "STOPPED", "FAILED"}
        await wait_for_runtime_condition(
            runtime,
            lambda: all(
                t.status in settled
                for t in runtime.list_threads(submission.execution_id)
                if t.name.startswith("sample-")
            ),
            timeout=30.0,
            message="the plate never settled after the crash",
        )
        statuses = {
            t.name: t.status for t in runtime.list_threads(submission.execution_id)
        }
        plate = {n: st for n, st in statuses.items() if n.startswith("sample-")}
        assert plate and all(st != "STOPPED" for st in plate.values()), (
            "the crash must not cancel the plate mid-journey and leave it "
            f"wherever the cancel landed; got {statuses}"
        )

        incidents = await runtime.incidents.list(category=IncidentCategory.THREAD_DIED)
        assert len(incidents) == 1, (
            f"a crashed thread must leave exactly one queryable record; got {incidents}"
        )
        detail = incidents[0].detail
        assert isinstance(detail, ThreadDiedContext)
        assert detail.labware_name.startswith("reservoir-"), (
            f"the incident has to name the labware left behind; got {detail}"
        )
        assert detail.error_type == "RuntimeError"
    finally:
        await runtime.shutdown(confirm=True)


@pytest.mark.timeout(90)
async def test_a_crash_on_an_unrelated_thread_leaves_the_others_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tip-rack-on-its-way-to-the-trash case. The spare plate shares no
    method, device or labware with the sample plate, so its death is nobody
    else's business: the sample plate runs its journey to the end."""
    build = await _build_system()

    original = ExecutingLabwareThread.initialize_labware

    async def crash_the_spare_thread(self: ExecutingLabwareThread) -> None:
        if self.thread_instance.labware.template_name == "spare":
            raise RuntimeError("simulated engine fault")
        await original(self)

    monkeypatch.setattr(
        ExecutingLabwareThread, "initialize_labware", crash_the_spare_thread)

    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    template = build.system.get_workflow_template("crash_wf")
    await runtime.start()
    try:
        submission = await runtime.submit(template, mode=WorkflowRunMode.PURE_SIM)
        await wait_for_runtime_condition(
            runtime,
            lambda: any(
                t.name.startswith("sample-") and t.status == "COMPLETED"
                for t in runtime.list_threads(submission.execution_id)
            ),
            timeout=45.0,
            message="the unrelated plate never finished its journey",
        )

        statuses = {
            t.name: t.status for t in runtime.list_threads(submission.execution_id)
        }
        spare = {n: st for n, st in statuses.items() if n.startswith("spare-")}
        assert spare and all(st == "FAILED" for st in spare.values()), (
            f"the crashed thread itself must land FAILED; got {statuses}"
        )

        incidents = await runtime.incidents.list(category=IncidentCategory.THREAD_DIED)
        named = [
            inc for inc in incidents
            if isinstance(inc.detail, ThreadDiedContext)
            and inc.detail.labware_name.startswith("spare-")
        ]
        assert len(named) == 1, (
            "the operator has to be told which labware was left behind and "
            f"where; incidents={[i.detail for i in incidents]}"
        )
    finally:
        await runtime.shutdown(confirm=True)
