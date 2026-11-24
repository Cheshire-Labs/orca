"""A plate its finished thread left on a start still holds that start for a joining submission."""

import asyncio

import pytest

import orca.orca as orca
from cheshire_drivers import CartesianCoordinates as C
from cheshire_drivers import Teachpoint
from orca.devices.venus import Venus
from orca.events.event_bus import EventBus
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.errors import StartLocationsOccupiedError
from orca.runtime.labware_service import LabwareService
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.system_runtime import SystemRuntime
from orca.runtime.blockers import BlockerKind
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from orca.sdk.workflow import WorkflowTemplate
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.labware_group import BarcodeAcquisition, LabwareGroup, LabwareGroupMember, PoolAcquisition
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.status_enums import RecoveryDecision
from orca.workflow_models.submission_modes import BatchMode
from orca.workflow_models.thread_context import ThreadContext
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.setup_package import package_stores, with_teachpoints, write_teachpoints
from tests.test_helpers import wait_until

_PLATE = "Cor_Falcon_96_wellplate_340ul_Fb_Black"


def _group(group_id: str, barcode: str) -> list[LabwareGroup]:
    return [LabwareGroup(id=group_id, members=(
        LabwareGroupMember("aborting_journey", BarcodeAcquisition(barcode=barcode)),
        LabwareGroupMember("open_journey", PoolAcquisition()),
    ))]


async def _world() -> tuple[SystemRuntime, WorkflowTemplate, asyncio.Event]:
    stores = package_stores()
    aborts = PlateTemplate("aborts", labware_type=_PLATE)
    keeps_run_open = PlateTemplate("keeps_run_open", labware_type=_PLATE)
    lh = Venus("lh", site_names=["s1", "s2"])
    names = ["start_a", "start_b", "spare", "end_a", "end_b", "lh/s1", "lh/s2"]
    arm = with_teachpoints(Transporter("arm"), write_teachpoints(stores, "arm", [
        Teachpoint(n, C(100 * i, 200, 300, 0, 90, 180), orientation="right") for i, n in enumerate(names)
    ]))
    release = asyncio.Event()

    @orca.action(device=lh, inputs=[keeps_run_open], deck_positions={keeps_run_open: "s2"})
    async def wait_for_release(ctx: ActionContext) -> None:
        await release.wait()

    @orca.method
    async def hold(ctx: MethodContext):
        yield wait_for_release

    @orca.thread(labware=aborts, start="start_a", end="end_a")
    async def aborting_journey(ctx: ThreadContext):
        raise RuntimeError("fails before it moves")
        yield hold

    @orca.thread(labware=keeps_run_open, start="start_b", end="end_b")
    async def open_journey(ctx: ThreadContext):
        yield hold

    @orca.workflow(name="abort_wf")
    def workflow(wf):
        wf.start(aborting_journey)
        wf.start(open_journey)

    topology = Topology(locations={**{n: PlatePad(n) for n in names[:5]}, "lh": lh}, transporters=[arm])
    build = await orca.build_system(
        name="Abort", workflow=workflow, topology=topology, stores=stores,
        event_bus=EventBus(), ops_history_store=memory_ops_store(),
    )
    runtime = await memory_runtime(
        build.system, event_bus=build.event_bus, labware_service=LabwareService(InMemoryLabwareStore()),
    )
    await runtime.start()
    await runtime.labware.register("aborts", location="start_a", barcode="A-1", confirm=True)
    await runtime.labware.register("aborts", location="spare", barcode="A-2", confirm=True)
    return runtime, build.system.get_workflow_template("abort_wf"), release


async def _abort_the_paused_thread(runtime: SystemRuntime, execution_id: str) -> None:
    await wait_until(lambda: bool(runtime.get_paused_threads(execution_id)), timeout=10.0)
    paused = runtime.get_paused_threads(execution_id)[0]
    runtime.recover_thread(execution_id, paused.id, RecoveryDecision.ABORT_THREAD)
    await wait_until(
        lambda: any(t.id == paused.id and t.status == "ABORTED" for t in runtime.list_threads(execution_id)),
        timeout=10.0,
    )


@pytest.mark.asyncio
async def test_a_join_is_refused_when_an_aborted_threads_plate_holds_the_start() -> None:
    runtime, template, release = await _world()
    try:
        first = await runtime.submit(
            template, groups=_group("g1", "A-1"), batch_mode=BatchMode.JOIN_EXISTING, mode=WorkflowRunMode.SIM,
        )
        await _abort_the_paused_thread(runtime, first.execution_id)
        await wait_until(lambda: any(
            t.name.startswith("keeps_run_open") and t.status == "EXECUTING_ACTION"
            for t in runtime.list_threads(first.execution_id)
        ), timeout=10.0)

        with pytest.raises(StartLocationsOccupiedError, match="start_a"):
            await runtime.submit(
                template, groups=_group("g2", "A-2"), batch_mode=BatchMode.JOIN_EXISTING, mode=WorkflowRunMode.SIM,
            )
    finally:
        release.set()
        await runtime.shutdown(confirm=True)


@pytest.mark.asyncio
async def test_a_plate_waiting_behind_an_aborted_threads_plate_says_it_waits_for_the_start() -> None:
    runtime, template, release = await _world()
    try:
        groups = [*_group("g1", "A-1"), *_group("g2", "A-2")]
        submission = await runtime.submit(template, groups=groups, mode=WorkflowRunMode.SIM)
        await _abort_the_paused_thread(runtime, submission.execution_id)
        await wait_until(
            lambda: any(t.waiting_for == "start_a" for t in runtime.list_threads(submission.execution_id)),
            timeout=10.0,
        )
        kinds = [blocker.kind for blocker in await runtime.blockers()]
        assert BlockerKind.WAITING_ON_UNOWNED_LABWARE in kinds
    finally:
        release.set()
        await runtime.shutdown(confirm=True)
