"""Operator Stop (call 1) freezes the whole run, threads that would be spawned after it too.

The pause fan-out only reaches the threads that exist when it runs. A parent
blocked on a reservation when the stop lands pauses where it waits, so it
never takes the device and never spawns the contributors it would have
started. A thread born into a held run is held from birth (the entry-thread
test below).
"""

from orca.config import OrcaConfig
import asyncio
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.events.event_bus import EventBus
from orca.resource_models.resource_pool import ResourcePool
from orca.modes.run_modes import WorkflowRunMode
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
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.memory_activity import memory_unsaved_positions
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service

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


async def _build_system() -> tuple[
    SystemRuntime, WorkflowTemplate, _HangingDevice, _CountingDevice,
]:
    """A hog thread holds dev1 inside a hanging action while the owner queues
    behind it for a shared action. The owner has not spawned its contributor
    yet, so a stop landing here has nothing to fan out to.

    Both are entry threads, so either could reach dev1 first. The owner waits
    on a gate device until the hog is inside its action, so the hog always wins.

    The contributor runs a prep step of its own on dev2 before it joins, so
    what it does when it starts is its own work rather than something the
    owner's pause would have parked anyway."""
    device = _HangingDevice("dev1", site_names=["site-1", "site-2"])
    prep_device = _CountingDevice("dev2")
    gate = UniversalMockDevice("gate")
    transporter = create_test_transporter(
        "robot1", ["dev1", "dev2", "gate", "pad1", "pad2", "pad3"],
    )
    plate_hog = create_test_plate_template("plate_hog")
    plate_main = create_test_plate_template("plate_main")
    plate_child = create_test_plate_template("plate_child")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(prep_device)
    registry.add_resource(gate)
    registry.add_resource(transporter)
    pool = ResourcePool("dev1", [device])
    prep_pool = ResourcePool("dev2", [prep_device])
    gate_pool = ResourcePool("gate", [gate])
    registry.add_resource_pool(pool)
    registry.add_resource_pool(prep_pool)
    registry.add_resource_pool(gate_pool)
    system_map = SystemMap(registry)
    await wire_system_map(
        system_map,
        devices={"dev1": device, "dev2": prep_device, "gate": gate},
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

    @orca.action(device=gate_pool, inputs=[plate_main])
    async def wait_for_the_hog(ctx: ActionContext) -> None:
        del ctx
        await device.in_shake.wait()

    @orca.method
    async def gate_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield wait_for_the_hog

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
        yield gate_method
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
        event_bus=event_bus, config=OrcaConfig(), ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(),
        unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    runtime = await memory_runtime(builder.get_system(), event_bus=event_bus)
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
async def test_an_owner_waiting_for_a_device_when_the_stop_lands_pauses_and_spawns_nothing() -> None:
    runtime, workflow, device, prep_device = await _build_system()
    await runtime.start()
    submission = await runtime.submit(workflow, mode=WorkflowRunMode.CHECK_RUN)
    eid = submission.execution_id
    await _run_until_owner_queues_behind_the_hog(runtime, eid, device)
    try:
        outcome = await runtime.stop_execution(eid)
        assert outcome.armed is True

        # The hog finishing its command frees nothing: it holds between commands.
        # The owner, waiting for the device, pauses where it waits.
        device.release.set()
        await wait_for_runtime_condition(
            runtime,
            lambda: not _still_running(runtime, eid),
            timeout=30.0,
            message="the run never settled after the stop",
        )

        [owner] = [t for t in runtime.list_threads(eid) if t.name.startswith("plate_main")]
        assert owner.status == "PAUSED"
        # "manual" and not "error": it is held, not parked on a failure.
        assert owner.pause_reason == "manual", owner.last_error
        assert not [t for t in runtime.list_threads(eid) if t.name.startswith("plate_child")], (
            "the owner spawned its contributor on a stopped run"
        )
        assert prep_device.shakes == 0, "a contributor ran its own step anyway"
    finally:
        device.release.set()
        await runtime.shutdown()


@pytest.mark.timeout(90)
async def test_resume_lets_the_paused_owner_spawn_its_contributor_and_finish() -> None:
    """The hold is not a one-way door: resume lets the paused owner take the
    device, spawn its contributor, and the run finishes."""
    runtime, workflow, device, prep_device = await _build_system()
    await runtime.start()
    submission = await runtime.submit(workflow, mode=WorkflowRunMode.CHECK_RUN)
    eid = submission.execution_id
    await _run_until_owner_queues_behind_the_hog(runtime, eid, device)
    try:
        await runtime.stop_execution(eid)
        device.release.set()
        await wait_for_runtime_condition(
            runtime,
            lambda: not _still_running(runtime, eid),
            timeout=30.0,
            message="the run never settled after the stop",
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
    submission = await runtime.submit(workflow, mode=WorkflowRunMode.CHECK_RUN)
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
    submission = await runtime.submit(workflow, mode=WorkflowRunMode.CHECK_RUN)
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
