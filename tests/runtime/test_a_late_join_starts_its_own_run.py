"""A JOIN_EXISTING submission that arrives after its host has finished.

A run ends when its threads drain, so a submission asking to join one can land
just as that happens. It was accepted anyway: the plate ran, but the execution
had already been reported COMPLETED and unregistered from the event forwarder,
so nothing the plate did reached the event bus. The operator's UI showed a
plate that never moved, and the run log has no trail of it.

A host that is no longer taking work is not a host: the submission starts its
own run, which is what `submit` already documents for a JOIN_EXISTING with no
live execution to join.
"""

from orca.events.event_bus import EventBus
import orca.orca as orca
from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig, Teachpoint
from cheshire_drivers import CartesianCoordinates as C
from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.transporter import Transporter
from orca.workflow_models.labware_group import LabwareGroup, LabwareGroupMember
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.sinks import CollectorSink
from orca.workflow_models.submission import BatchMode
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from orca.spawn import DISPENSE
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from tests.test_helpers import run_to_quiescence
from tests.setup_package import package_stores, write_deck_layouts, write_teachpoints
from tests.setup_package import with_teachpoints, with_deck_layouts
from tests.memory_activity import memory_ops_store, memory_runtime


_DECK_CONFIG = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7)],
)


async def _build_one_plate_system():
    stores = package_stores()
    sample = PlateTemplate("sample", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")

    lh = with_deck_layouts(LiquidHandler(
        "lh",
        deck_layout="default",
    ), write_deck_layouts(stores, "lh", {"default": _DECK_CONFIG}))
    stacker = Storage("stacker")
    waste = Storage("waste")
    arm = with_teachpoints(Transporter(
        "arm",
    ), write_teachpoints(stores, "arm", [
            Teachpoint("stacker", C(0, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint("lh/carrier-7-2", C(400, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint("waste", C(600, 200, 300, 0, 90, 180), orientation="right"),
        ]))

    @orca.action(device=lh, inputs=[sample], deck_positions={sample: "carrier-7-0"})
    async def touch(ctx: ActionContext) -> None:
        ctx.labware("sample")

    @orca.method
    async def touch_method(ctx: MethodContext):
        yield touch

    @orca.thread(labware=sample, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield touch_method

    @orca.workflow(name="late_join_wf")
    def workflow(wf):
        wf.start(plate_journey)

    topology = Topology(
        locations={"stacker": stacker, "lh": lh, "waste": waste}, transporters=[arm])
    return await orca.build_system(
        name="Late Join", workflow=workflow, topology=topology, stores=stores, event_bus=EventBus(), ops_history_store=memory_ops_store())


def _group(group_id: str) -> LabwareGroup:
    return LabwareGroup(
        id=group_id,
        members=(LabwareGroupMember(thread_template_name="plate_journey"),),
    )


async def test_a_join_arriving_after_its_host_finished_starts_its_own_run() -> None:
    build = await _build_one_plate_system()
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    sink = CollectorSink()
    runtime.register_sink(sink)
    template = build.system.get_workflow_template("late_join_wf")
    await runtime.start()
    try:
        first = await runtime.submit(
            template, groups=[_group("g1")], mode=WorkflowRunMode.CHECK_RUN)
        first_statuses = await run_to_quiescence(runtime, first.execution_id, timeout=60.0)
        assert first_statuses and all(s == "COMPLETED" for s in first_statuses.values()), (
            f"the host run must finish before the late join; got {first_statuses}"
        )

        second = await runtime.submit(
            template,
            groups=[_group("g2")],
            batch_mode=BatchMode.JOIN_EXISTING,
            mode=WorkflowRunMode.CHECK_RUN,
        )
        assert second.execution_id != first.execution_id, (
            "a finished run is not a host: the late join must start its own"
        )
        second_statuses = await run_to_quiescence(runtime, second.execution_id, timeout=60.0)
    finally:
        await runtime.shutdown(confirm=True)

    assert second_statuses and all(s == "COMPLETED" for s in second_statuses.values()), (
        f"the late join's plate must run to completion; got {second_statuses}"
    )
    thread_events = [
        event for event in sink.events
        if event.execution_id == second.execution_id and event.event_name.startswith("THREAD.")
    ]
    assert thread_events, (
        "the late join's plate moved without a single thread event reaching the "
        "bus: the UI and the ops archive would show a plate that never moved"
    )
