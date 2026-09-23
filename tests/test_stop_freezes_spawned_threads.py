"""Operator Stop (call 1) freezes the whole run, threads spawned after it too.

The pause fan-out only reaches the threads that exist when it runs. A parent
blocked on a reservation when the stop lands still spawns its contributors on
the way to its own safe point, so those threads are born after the fan-out --
and used to start moving labware on a run the operator had stopped.
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.events.event_bus import EventBus
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.sinks import CollectorSink
from orca.runtime.status_models import ThreadSnapshot
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.mock import UniversalMockDevice
from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    wait_for_runtime_condition,
    wire_system_map,
)

_TERMINAL = ("COMPLETED", "ABORTED", "STOPPED", "FAILED")


class _CountingDevice(UniversalMockDevice):
    """Counts the work asked of it, so a test can say "nothing ran here"."""

    def __init__(self, name: str, site_names: list[str] | None = None) -> None:
        super().__init__(name, site_names=site_names)
        self.shakes = 0

    async def shake(self, duration: int, speed: int) -> None:
        self.shakes += 1
        await super().shake(duration, speed)


class _HangingDevice(UniversalMockDevice):
    def __init__(self, name: str, site_names: list[str] | None = None) -> None:
        super().__init__(name, site_names=site_names)
        self.in_shake = asyncio.Event()
        self.release = asyncio.Event()

    async def shake(self, duration: int, speed: int) -> None:
        self.in_shake.set()
        await self.release.wait()
        await super().shake(duration, speed)


def _statuses(runtime: SystemRuntime, eid: str) -> dict[str, str]:
    return {t.name: t.status for t in runtime.list_threads(eid)}


def _still_running(runtime: SystemRuntime, eid: str) -> dict[str, str]:
    """Threads that are neither finished nor parked."""
    return {
        name: status for name, status in _statuses(runtime, eid).items()
        if status not in _TERMINAL and status != "PAUSED"
    }


def _contributor(runtime: SystemRuntime, eid: str) -> ThreadSnapshot | None:
    """The spawned contributor once it is past CREATED, else None.

    CREATED is where both worlds start, so waiting past it is what makes the
    assertion mean anything: held, the thread goes straight to PAUSED; unheld,
    it walks off toward its first move.
    """
    for snap in runtime.list_threads(eid):
        if snap.name.startswith("plate_child") and snap.status != "CREATED":
            return snap
    return None


async def _build_system() -> tuple[
    SystemRuntime, WorkflowTemplate, _HangingDevice, _CountingDevice,
]:
    """A hog thread holds dev1 inside a hanging action while the owner queues
    behind it for a shared action. The owner has not spawned its contributor
    yet, so a stop landing here has nothing to fan out to.

    The contributor runs a prep step of its own on dev2 before it joins, so
    what it does when it starts is its own work rather than something the
    owner's pause would have parked anyway."""
    device = _HangingDevice("dev1", site_names=["site-1", "site-2"])
    prep_device = _CountingDevice("dev2")
    transporter = create_test_transporter(
        "robot1", ["dev1", "dev2", "pad1", "pad2", "pad3"],
    )
    plate_hog = create_test_plate_template("plate_hog")
    plate_main = create_test_plate_template("plate_main")
    plate_child = create_test_plate_template("plate_child")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(prep_device)
    registry.add_resource(transporter)
    pool = ResourcePool("dev1", [device])
    prep_pool = ResourcePool("dev2", [prep_device])
    registry.add_resource_pool(pool)
    registry.add_resource_pool(prep_pool)
    system_map = SystemMap(registry)
    await wire_system_map(
        system_map,
        devices={"dev1": device, "dev2": prep_device},
        pads=["pad1", "pad2", "pad3"],
    )

    @orca.action(device=pool, inputs=[plate_hog])
    async def hog_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.action(device=pool, inputs=[plate_main, plate_child])
    async def shared_action(ctx: ActionContext) -> None:
        await ctx.device().seal(temperature=20, duration=1)

    @orca.method
    async def hog_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield hog_action

    @orca.action(device=prep_pool, inputs=[plate_child])
    async def prep_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def owner_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shared_action

    @orca.method
    async def prep_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield prep_action

    pad1 = system_map.get_location("pad1")
    pad2 = system_map.get_location("pad2")
    pad3 = system_map.get_location("pad3")

    @orca.thread(labware=plate_hog, start=pad1, end=pad1)
    async def hog_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield hog_method

    @orca.thread(labware=plate_main, start=pad2, end=pad2)
    async def owner_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield owner_method

    @orca.thread(labware=plate_child, start=pad3, end=pad3)
    async def child_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield prep_method
        yield orca.join()

    @orca.workflow(name="spawn_after_stop_wf")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(hog_thread)
        wf.start(owner_thread)
        wf.thread(child_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system",
        description="",
        labwares=[plate_hog, plate_main, plate_child],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    runtime = SystemRuntime(builder.get_system(), event_bus=event_bus)
    return runtime, workflow, device, prep_device


async def _run_until_owner_queues_behind_the_hog(
    runtime: SystemRuntime, eid: str, device: _HangingDevice,
) -> None:
    """Park the run in the window the bug needs: the hog inside its hanging
    action, the owner waiting on the device it holds and so not yet spawned."""
    await asyncio.wait_for(device.in_shake.wait(), timeout=20.0)
    await wait_for_runtime_condition(
        runtime,
        lambda: "AWAITING_ACTION_RESERVATION" in _statuses(runtime, eid).values(),
        timeout=30.0,
        message="the owner never queued behind the hog for the device",
    )


@pytest.mark.timeout(90)
async def test_stop_freezes_threads_spawned_after_the_stop() -> None:
    runtime, workflow, device, prep_device = await _build_system()
    collector = CollectorSink()
    runtime.register_sink(collector)
    await runtime.start()
    submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
    eid = submission.execution_id
    await _run_until_owner_queues_behind_the_hog(runtime, eid, device)
    try:
        outcome = await runtime.stop_execution(eid)
        assert outcome.armed is True

        # Draining the hog lets the owner take the device and spawn the
        # contributor: the thread born after the fan-out, so the one at risk.
        device.release.set()
        with contextlib.suppress(TimeoutError):
            await wait_for_runtime_condition(
                runtime,
                lambda: _contributor(runtime, eid) is not None
                and not _still_running(runtime, eid),
                timeout=30.0,
            )
        assert not _still_running(runtime, eid), _statuses(runtime, eid)

        contributor = _contributor(runtime, eid)
        assert contributor is not None, "the owner never spawned its contributor"
        assert contributor.status == "PAUSED"
        # "manual" and not "error": it is held, not parked on a failure.
        assert contributor.pause_reason == "manual", contributor.last_error

        # Where it ENDS up is not the point, since an unheld contributor also
        # parks eventually, once it is standing at the shared action's device
        # with its plate already moved. What has to hold is that it was never
        # running at all: nothing between being created and being held.
        walked = [
            e.status for e in collector.events if e.entity_id == contributor.id
        ]
        assert set(walked) <= {"CREATED", "PAUSED"}, walked
        assert prep_device.shakes == 0, "the contributor ran its own step anyway"
    finally:
        device.release.set()
        await runtime.shutdown()


@pytest.mark.timeout(90)
async def test_resume_releases_a_thread_spawned_during_the_stop() -> None:
    """The hold is not a one-way door: resume lets the born-paused contributor
    run, and the run finishes."""
    runtime, workflow, device, prep_device = await _build_system()
    await runtime.start()
    submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
    eid = submission.execution_id
    await _run_until_owner_queues_behind_the_hog(runtime, eid, device)
    try:
        await runtime.stop_execution(eid)
        device.release.set()
        await wait_for_runtime_condition(
            runtime,
            lambda: getattr(_contributor(runtime, eid), "status", None) == "PAUSED",
            timeout=30.0,
            message="the contributor was never spawned and held",
        )

        runtime.resume_execution(eid)
        final = await asyncio.wait_for(runtime.wait(eid), timeout=60.0)
        assert final.status == ExecutionState.COMPLETED, _statuses(runtime, eid)
    finally:
        device.release.set()
        await runtime.shutdown()


@pytest.mark.timeout(90)
async def test_stop_right_after_submit_holds_the_entry_threads() -> None:
    """A stop can land before the workflow has even attached. The entry threads
    are started by a different path than the contributors, and they carry the
    run's starting plates, so they have to honour the hold too."""
    runtime, workflow, device, prep_device = await _build_system()
    await runtime.start()
    submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
    eid = submission.execution_id
    outcome = await runtime.stop_execution(eid)
    try:
        assert outcome.armed is True

        await wait_for_runtime_condition(
            runtime,
            lambda: bool(_statuses(runtime, eid)) and not _still_running(runtime, eid),
            timeout=30.0,
            message="the run never settled after a stop issued at submit time",
        )
        assert not device.in_shake.is_set(), (
            "an entry thread reached the device on a run stopped at submit time"
        )
        assert set(_statuses(runtime, eid).values()) == {"PAUSED"}, _statuses(runtime, eid)
    finally:
        device.release.set()
        await runtime.shutdown()


@pytest.mark.timeout(90)
async def test_settling_a_device_timeout_does_not_undo_an_operator_stop() -> None:
    """`resume_all_threads` is what the recoverable-timeout coordinator calls
    once an operator settles a timed-out device call. An operator stop outranks
    that: the run stays stopped and its threads stay held."""
    runtime, workflow, device, prep_device = await _build_system()
    await runtime.start()
    submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
    eid = submission.execution_id
    await _run_until_owner_queues_behind_the_hog(runtime, eid, device)
    try:
        await runtime.stop_execution(eid)
        device.release.set()
        await wait_for_runtime_condition(
            runtime,
            lambda: bool(_statuses(runtime, eid)) and not _still_running(runtime, eid),
            timeout=30.0,
            message="the run never settled after the stop",
        )

        counters = runtime.resume_all_threads(eid)

        assert counters == {
            "resumed": 0, "pause_cancelled": 0,
            "error_skipped": 0, "completed_skipped": 0,
        }
        assert not _still_running(runtime, eid), _statuses(runtime, eid)
        assert runtime.require_execution(eid).is_paused is True
    finally:
        device.release.set()
        await runtime.shutdown()
