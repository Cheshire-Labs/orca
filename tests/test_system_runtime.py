"""Tests for SystemRuntime -- the persistent runtime wrapping a built System.

TDD: These tests define the expected API. Implementation follows.
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

from orca.resource_models.resource_pool import ResourcePool
from orca.sdk.events import EventBus
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.system.system_interface import ISystem
import orca.orca as orca
from orca.runtime.system_runtime import SystemRuntime, RuntimeState, ExecutionRecord, ExecutionState
from orca.runtime.execution import ExecutionPhase
from orca.runtime.status_models import ExecutionDetail, ThreadSnapshot
from orca.runtime.run_modes import WorkflowRunMode
from tests.test_helpers import (
    wire_system_map,
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
)


async def _build_simple_system() -> tuple[ISystem, WorkflowTemplate]:
    """Build a minimal sim system with one device, one transporter, two workflows.

    The primary workflow shakes a plate at pad1 once and returns it. The
    secondary workflow `simple_workflow_b` is the same shape but starts/ends
    at pad2 and uses a separate labware template (`plate_96_b`). Daemon
    route tests that need two concurrent executions submit it for exec_b so
    pad1's mid-flight plate from exec_a does not collide on the start-location
    pre-submit start_location check (which refuses two executions claiming
    the same start_location).

    Returns `(system, workflow)` for the primary workflow; existing callers
    that ignore the secondary workflow continue to work unchanged.
    """
    device = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1", "pad2"])
    plate = create_test_plate_template("plate_96")
    plate_b = create_test_plate_template("plate_96_b")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)

    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map, devices={"shaker1": device}, pads=["pad1", "pad2"],
    )

    @orca.action(device=pool, inputs=[plate])
    async def shake_action(ctx: object) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_method(ctx: object) -> AsyncGenerator[object, None]:
        yield shake_action

    @orca.action(device=pool, inputs=[plate_b])
    async def shake_action_b(ctx: object) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_method_b(ctx: object) -> AsyncGenerator[object, None]:
        yield shake_action_b

    pad_loc = system_map.get_location("pad1")
    pad2_loc = system_map.get_location("pad2")

    @orca.thread(labware=plate, start=pad_loc, end=pad_loc)
    async def plate_thread(ctx: object) -> AsyncGenerator[MethodTemplate, None]:
        yield shake_method
    thread = plate_thread

    @orca.thread(labware=plate_b, start=pad2_loc, end=pad2_loc)
    async def plate_thread_b(ctx: object) -> AsyncGenerator[MethodTemplate, None]:
        yield shake_method_b
    thread_b = plate_thread_b

    workflow = WorkflowTemplate("simple_workflow")
    workflow.add_thread(thread, is_start=True)

    workflow_b = WorkflowTemplate("simple_workflow_b")
    workflow_b.add_thread(thread_b, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system",
        description="",
        labwares=[plate, plate_b],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow, workflow_b],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    return system, workflow


class TestRuntimeLifecycleEnforcement:
    """Verify that the runtime enforces valid state transitions."""

    async def test_cannot_submit_before_start(self) -> None:
        """Submitting a workflow on a CREATED runtime must fail."""
        system, workflow = await _build_simple_system()
        runtime = SystemRuntime(system)
        with pytest.raises(RuntimeError, match="not running"):
            await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)

    async def test_cannot_submit_after_stop(self) -> None:
        """Submitting a workflow on a STOPPED runtime must fail."""
        system, workflow = await _build_simple_system()
        runtime = SystemRuntime(system)
        await runtime.start()
        await runtime.shutdown()
        assert runtime.state == RuntimeState.STOPPED
        with pytest.raises(RuntimeError, match="not running"):
            await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)


class TestWorkflowExecution:
    """Core execution tests: submit, wait, status, cancel."""

    async def test_submit_and_wait_completes(self) -> None:
        """The fundamental happy path: submit a workflow and wait for completion."""
        system, workflow = await _build_simple_system()
        runtime = SystemRuntime(system)
        await runtime.start()

        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        assert isinstance(record, ExecutionRecord)
        assert record.workflow_name == workflow.name

        status = await asyncio.wait_for(runtime.wait(record.id), timeout=30.0)
        assert status.status == ExecutionState.COMPLETED
        assert status.error is None
        await runtime.shutdown()

    async def test_execution_tracking(self) -> None:
        """Verify executions are tracked and queryable by ID and via list."""
        system, workflow = await _build_simple_system()
        runtime = SystemRuntime(system)
        await runtime.start()

        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)

        # Can query by ID immediately after submit
        status = runtime.get_execution(record.id)
        assert status.id == record.id
        assert status.workflow_name == workflow.name

        # Listed in all executions
        all_executions = runtime.list_executions()
        assert any(e.id == record.id for e in all_executions)

        await asyncio.wait_for(runtime.wait(record.id), timeout=30.0)
        await runtime.shutdown()

    async def test_abort_execution_transitions_through_stopping(self) -> None:
        """abort_execution sets ``STOPPING`` while threads cooperatively
        unwind; ``_on_task_done`` flips the phase to ``ABORTED`` once the
        task fully cancels. Pre-fix the phase jumped straight to ABORTED
        while threads_active was still > 0, misleading operators into
        thinking cleanup was safe.
        """
        system, workflow = await _build_simple_system()
        runtime = SystemRuntime(system)
        await runtime.start()

        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        # Precondition: the workflow shape (transporter move + shake_action
        # + move-back) is slow enough that submit returns with the task
        # still mid-flight. Guard against a future scheduler tick that
        # races to completion before we can issue the stop -- if the task
        # has already finished, this test can't observe the STOPPING
        # intermediate state and the assertion below would spuriously fail.
        execution = runtime._executions[record.id]
        assert not execution.task.done(), (
            "test premise broken: task finished before abort ran; "
            "the intermediate STOPPING phase only exists for mid-flight tasks"
        )
        await runtime.abort_execution(record.id)

        # Immediately after stop, phase is STOPPING (task cancel was
        # requested but cancellation has not yet propagated through the
        # task to _on_task_done).
        immediate = runtime.get_execution_status(record.id)
        assert immediate.status is ExecutionPhase.STOPPING

        # Flat-shim view treats STOPPING as RUNNING (still active).
        flat_immediate = runtime.get_execution(record.id)
        assert flat_immediate.status == ExecutionState.RUNNING

        # Await terminal -- _on_task_done transitions STOPPING -> ABORTED.
        # ``runtime.wait`` awaits the task itself; ``_on_task_done`` is a
        # done-callback scheduled via ``loop.call_soon``, which may run
        # AFTER the awaiter resumes. Yield once so the callback drains.
        await runtime.wait(record.id)
        await asyncio.sleep(0)
        terminal = runtime.get_execution_status(record.id)
        assert terminal.status is ExecutionPhase.ABORTED
        flat_terminal = runtime.get_execution(record.id)
        assert flat_terminal.status == ExecutionState.ABORTED

        await runtime.shutdown()

    async def test_abort_execution_on_finished_task_is_noop(self) -> None:
        """Review-finding: ``abort_execution`` called when the task is
        ``done()`` but before ``_on_task_done`` has flipped the phase
        must NOT briefly write STOPPING over the impending terminal
        value, AND must drain the pending done-callback before
        returning so callers reading ``get_execution_status`` see the
        terminal value, not the stale pre-callback ACCEPTING/DRAINING
        phase.
        """
        system, workflow = await _build_simple_system()
        runtime = SystemRuntime(system)
        await runtime.start()

        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        await runtime.wait(record.id)
        execution = runtime._executions[record.id]
        assert execution.task.done()

        await runtime.abort_execution(record.id)
        post_stop = runtime.get_execution_status(record.id)
        assert post_stop.status is ExecutionPhase.COMPLETED, (
            f"abort on a finished task must surface the terminal phase "
            f"(callback drained before return); got {post_stop.status}"
        )

        await runtime.shutdown()

    async def test_abort_execution_idempotent_on_terminal_phase(self) -> None:
        """Calling abort_execution on an already-finished execution does
        not regress its terminal phase back to STOPPING. The phase set
        by _on_task_done (COMPLETED/FAILED/ABORTED) is sticky against
        a follow-up stop request.
        """
        system, workflow = await _build_simple_system()
        runtime = SystemRuntime(system)
        await runtime.start()

        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        await runtime.wait(record.id)
        terminal = runtime.get_execution_status(record.id)
        assert terminal.status is ExecutionPhase.COMPLETED

        # Late abort -- workflow already finished. Phase should stay COMPLETED.
        await runtime.abort_execution(record.id)
        post = runtime.get_execution_status(record.id)
        assert post.status is ExecutionPhase.COMPLETED

        await runtime.shutdown()

    async def test_nonexistent_execution_raises(self) -> None:
        """Querying a non-existent execution ID raises KeyError."""
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system)
        await runtime.start()

        with pytest.raises(KeyError):
            runtime.get_execution("nonexistent-id")

        await runtime.shutdown()


class TestStatusQueries:
    """Thread and action-level status queries on SystemRuntime."""

    async def test_list_threads_during_execution(self) -> None:
        """Threads are visible shortly after submit."""
        system, workflow = await _build_simple_system()
        runtime = SystemRuntime(system)
        await runtime.start()

        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)

        # Poll until at least one thread appears (the background task needs a
        # turn on the event loop to create threads before they are queryable).
        for _ in range(20):
            threads = runtime.list_threads(record.id)
            if threads:
                break
            await asyncio.sleep(0.05)

        assert len(threads) > 0
        assert all(isinstance(t, ThreadSnapshot) for t in threads)

        await asyncio.wait_for(runtime.wait(record.id), timeout=30.0)
        await runtime.shutdown()

    async def test_execution_detail_after_completion(self) -> None:
        """After completion, all threads show COMPLETED status."""
        system, workflow = await _build_simple_system()
        runtime = SystemRuntime(system)
        await runtime.start()

        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        await asyncio.wait_for(runtime.wait(record.id), timeout=30.0)

        detail = runtime.get_execution_detail(record.id)
        assert isinstance(detail, ExecutionDetail)
        assert detail.status == "completed"
        assert detail.total_thread_count > 0
        assert detail.completed_thread_count == detail.total_thread_count
        assert detail.active_thread_count == 0
        for thread in detail.threads:
            assert thread.status == "COMPLETED"

        await runtime.shutdown()

    async def test_get_thread_detail_by_id(self) -> None:
        """Can query an individual thread by ID."""
        system, workflow = await _build_simple_system()
        runtime = SystemRuntime(system)
        await runtime.start()

        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        await asyncio.wait_for(runtime.wait(record.id), timeout=30.0)

        threads = runtime.list_threads(record.id)
        thread_id = threads[0].id

        detail = runtime.get_thread_detail(record.id, thread_id)
        assert detail.id == thread_id
        assert detail.status == "COMPLETED"

        await runtime.shutdown()

    async def test_thread_detail_not_found_raises(self) -> None:
        """KeyError for a bad thread ID."""
        system, workflow = await _build_simple_system()
        runtime = SystemRuntime(system)
        await runtime.start()

        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        await asyncio.wait_for(runtime.wait(record.id), timeout=30.0)

        with pytest.raises(KeyError, match="Thread"):
            runtime.get_thread_detail(record.id, "nonexistent-thread")

        await runtime.shutdown()

    async def test_execution_detail_not_found_raises(self) -> None:
        """KeyError for a bad execution ID."""
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system)
        await runtime.start()

        with pytest.raises(KeyError, match="Execution"):
            runtime.get_execution_detail("nonexistent-id")

        await runtime.shutdown()

    async def test_thread_snapshot_has_method_info(self) -> None:
        """After completion, thread snapshot reflects completed methods."""
        system, workflow = await _build_simple_system()
        runtime = SystemRuntime(system)
        await runtime.start()

        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        await asyncio.wait_for(runtime.wait(record.id), timeout=30.0)

        threads = runtime.list_threads(record.id)
        thread = threads[0]
        # The simple fixture has 1 method; after completion it's in completed_methods
        assert thread.completed_method_count == 1
        # current_method is None after completion
        assert thread.current_method is None

        await runtime.shutdown()
