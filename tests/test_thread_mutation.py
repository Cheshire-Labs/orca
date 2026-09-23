"""Tests for thread mutation API.

Tests cover:
- Raw list operations on ExecutingLabwareThread
- ThreadMutationHandler guards (duplicate name, completed thread)
- Integration: handler-driven mutation during workflow execution
- thread_id propagation in MethodExecutionContext
- SMC assay E2E with mutation (slow)
"""

import asyncio
from dataclasses import dataclass
from typing import AsyncGenerator

import pytest

from orca.state.ops_store import SYSTEM_ID

import orca.orca as orca
from orca.events.event_handler_interface import IEventHandler
from orca.events.execution_context import ExecutionContext, MethodExecutionContext, ThreadExecutionContext
from orca.plugins import MethodTracker
from orca.resource_models.devices import Device
from orca.resource_models.labware import LabwareTemplate
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.device_factory import SimDeviceFactory
from orca.runtime.sinks import CollectorSink
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.events import EventBus
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate, MethodTemplate
from orca.workflow_models.mutation_position import AtHead, AtTail
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import ThreadTemplate
from orca.runtime.run_modes import WorkflowRunMode
from tests.mock import UniversalMockDevice
from tests.mutation_helpers import wait_for_threads, pause_and_wait, wait_for_paused
from tests.test_helpers import create_test_plate_template, create_test_transporter, wire_system_map



# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@dataclass
class Fixture:
    runtime: SystemRuntime
    workflow: WorkflowTemplate
    device: UniversalMockDevice
    event_bus: EventBus
    plate: LabwareTemplate
    pool: ResourcePool


async def _build_system(
    method1_name: str = "shake_method",
    method2_name: str = "seal_method",
) -> Fixture:
    device = UniversalMockDevice("device1")
    transporter = create_test_transporter("robot1", ["device1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)

    pool = ResourcePool("device1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate])
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.action(device=pool, inputs=[plate])
    async def seal_action(ctx: ActionContext) -> None:
        await ctx.device().seal(temperature=180, duration=3)

    async def _m1_gen(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_action

    async def _m2_gen(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield seal_action

    m1 = MethodTemplate(method1_name, func=_m1_gen)
    m2 = MethodTemplate(method2_name, func=_m2_gen)

    pad_loc = system_map.get_location("pad1")

    async def _thread_gen(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield m1
        yield m2

    thread = ThreadTemplate(labware_template=plate, start=pad_loc, end=pad_loc, func=_thread_gen)

    workflow = WorkflowTemplate("test_workflow")
    workflow.add_thread(thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[plate],
        resources_registry=registry, system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    runtime = SystemRuntime(system, event_bus=event_bus)
    return Fixture(runtime, workflow, device, event_bus, plate, pool)


# ---------------------------------------------------------------------------
# Unit tests: raw mutation operations
# ---------------------------------------------------------------------------



class TestRawMutationOperations:

    async def test_append_method_adds_to_end_and_executes(self) -> None:
        """Append a method while paused. After resume, all methods including
        the appended one must execute and the workflow completes."""
        f = await _build_system()
        collector = CollectorSink()
        f.runtime.register_sink(collector)
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)
        thread = f.runtime.system.get_executing_thread(threads[0].id)

        await pause_and_wait(f.runtime, record.id, threads[0].id)

        @orca.action(device=f.pool, inputs=[f.plate])
        async def extra_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=100)

        @orca.method
        async def extra_shake(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield extra_action

        f.runtime.system.insert_method(threads[0].id, extra_shake, where=AtTail())

        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED, "Workflow should complete after appended method executes"

        completed_methods = [
            e.context.method_name for e in collector.events
            if e.entity_type == "METHOD" and e.status == "COMPLETED"
            and isinstance(e.context, MethodExecutionContext)
        ]
        assert "extra_shake" in completed_methods, (
            f"Appended method 'extra_shake' should have executed. Completed: {completed_methods}"
        )
        await f.runtime.shutdown()

    async def test_insert_method_next_runs_before_remaining(self) -> None:
        """Insert a method at the front while paused. After resume, the
        inserted method must execute before the remaining pending methods."""
        f = await _build_system()
        collector = CollectorSink()
        f.runtime.register_sink(collector)
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)
        thread = f.runtime.system.get_executing_thread(threads[0].id)

        await pause_and_wait(f.runtime, record.id, threads[0].id)

        @orca.action(device=f.pool, inputs=[f.plate])
        async def urgent_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=100)

        @orca.method
        async def urgent_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield urgent_action

        f.runtime.system.insert_method(threads[0].id, urgent_method, where=AtHead())

        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED

        completed_methods = [
            e.context.method_name for e in collector.events
            if e.entity_type == "METHOD" and e.status == "COMPLETED"
            and isinstance(e.context, MethodExecutionContext)
        ]
        assert "urgent_method" in completed_methods, (
            f"Inserted method should have executed. Completed: {completed_methods}"
        )
        # Verify ordering: urgent_method completed before seal_method
        urgent_idx = completed_methods.index("urgent_method")
        seal_idx = completed_methods.index("seal_method")
        assert urgent_idx < seal_idx, (
            f"urgent_method should run before seal_method. "
            f"Order: {completed_methods}"
        )
        await f.runtime.shutdown()


# ---------------------------------------------------------------------------
# Guard tests
# ---------------------------------------------------------------------------

class TestMutationGuards:

    async def test_completed_thread_rejects_mutation(self) -> None:
        f = await _build_system()
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=30.0)
        assert status.status == ExecutionState.COMPLETED

        threads = f.runtime.list_threads(record.id)
        @orca.action(device=f.pool, inputs=[f.plate])
        async def late_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=100)

        @orca.method
        async def late_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield late_action

        extra = late_method
        with pytest.raises(ValueError, match="cannot accept mutations"):
            f.runtime.system.insert_method(threads[0].id, extra, where=AtTail())
        await f.runtime.shutdown()

    async def test_duplicate_name_skip_skips_first_occurrence(self) -> None:
        """With two methods of the same name, skip_pending_method skips the first
        occurrence and the second still executes."""
        f = await _build_system()
        collector = CollectorSink()
        f.runtime.register_sink(collector)
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)

        await pause_and_wait(f.runtime, record.id, threads[0].id)

        @orca.action(device=f.pool, inputs=[f.plate])
        async def dup_seal(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=180, duration=3)

        async def _dup_gen(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield dup_seal

        dup = MethodTemplate("seal_method", func=_dup_gen)
        f.runtime.system.insert_method(threads[0].id, dup, where=AtTail())

        # Skips the first seal_method; the duplicate still runs
        f.runtime.system.skip_pending_method(threads[0].id, method_name="seal_method")
        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        thread = f.runtime.system.get_executing_thread(threads[0].id)
        seal_methods = [m for m in thread.completed_methods if m.name == "seal_method"]
        skipped_seals = [m for m in seal_methods if m.was_skipped]
        executed_seals = [m for m in seal_methods if not m.was_skipped]
        assert len(skipped_seals) == 1, "First seal_method should be skipped"
        assert len(executed_seals) == 1, "Second seal_method (dup) should execute"
        await f.runtime.shutdown()


# ---------------------------------------------------------------------------
# thread_id in MethodExecutionContext
# ---------------------------------------------------------------------------

class TestThreadIdInContext:

    async def test_method_completed_event_has_thread_id(self) -> None:
        f = await _build_system()
        collector = CollectorSink()
        f.runtime.register_sink(collector)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        await asyncio.wait_for(f.runtime.wait(record.id), timeout=30.0)
        await f.runtime.shutdown()

        method_events = [
            e for e in collector.events
            if e.entity_type == "METHOD" and e.status == "COMPLETED"
        ]
        assert len(method_events) >= 1
        for event in method_events:
            ctx = event.context
            if isinstance(ctx, MethodExecutionContext):
                assert ctx.thread_id is not None, "METHOD.COMPLETED should have thread_id"

    async def test_action_completed_event_has_thread_id(self) -> None:
        """``ExecutingMethod._create_executable_action`` previously built
        a ``MethodExecutionContext`` without
        ``thread_id`` / ``thread_name`` / ``participating_thread_ids``.
        The action's status-change events and the
        ``DeclaredTrackingObserver`` saw an empty
        ``participating_thread_ids`` tuple, so every ops_history record
        landed with ``thread_id=""`` and the search-by-thread filter was
        a no-op. Pin the contract: every ACTION.COMPLETED event now
        carries the owning thread id.
        """
        f = await _build_system()
        collector = CollectorSink()
        f.runtime.register_sink(collector)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        await asyncio.wait_for(f.runtime.wait(record.id), timeout=30.0)
        await f.runtime.shutdown()

        action_events = [
            e for e in collector.events
            if e.entity_type == "ACTION" and e.status == "COMPLETED"
        ]
        assert len(action_events) >= 1
        for event in action_events:
            ctx = event.context
            if isinstance(ctx, MethodExecutionContext):
                assert ctx.thread_id is not None, (
                    "ACTION.COMPLETED context.thread_id is None -- "
                    "MethodExecutionContext was built without thread fields"
                )
                assert ctx.participating_thread_ids, (
                    "ACTION.COMPLETED context.participating_thread_ids is "
                    "empty -- ops_history records would land with "
                    "thread_id=''"
                )


# ---------------------------------------------------------------------------
# Submission lifecycle: IN_PROGRESS transition timing
#
# STANDALONE submissions sat at ACCEPTED for 80+ seconds before flipping
# to IN_PROGRESS. Root cause:
# ``ExecutingWorkflow.start()`` awaited ``asyncio.gather(*entry threads)``,
# which only returns when every entry thread terminates. The
# ``ACCEPTED -> IN_PROGRESS`` transition was sequenced AFTER ``start()``
# returned, so it landed minutes late on slow hardware.
#
# Fix: ``start()`` now schedules entry threads as tasks, signals
# ``entry_threads_started``, then awaits gather. ``_run_workflow`` gates the
# IN_PROGRESS transition on the event so the wire signal arrives promptly.
# ---------------------------------------------------------------------------


class TestSubmissionInProgressTransitionTiming:

    async def test_in_progress_fires_before_entry_threads_complete(self) -> None:
        """The submission must transition ACCEPTED -> IN_PROGRESS while entry
        threads are still running, not after they complete. Pre-fix the
        transition only fired after ``executing_workflow.start()`` returned,
        which awaited every entry thread to terminal -- 80s+ on real
        hardware. Use a slow shake action so a late transition is loud:
        if the test sees IN_PROGRESS only AFTER the action sleep window,
        the timing fix has regressed.
        """
        from orca.runtime.submission import SubmissionStatus

        device = UniversalMockDevice("device1")
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_96")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)

        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)

        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

        action_started = asyncio.Event()
        release_action = asyncio.Event()

        @orca.action(device=pool, inputs=[plate])
        async def slow_action(ctx: ActionContext) -> None:
            action_started.set()
            await release_action.wait()

        async def _m_gen(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield slow_action

        m = MethodTemplate("slow_method", func=_m_gen)
        pad_loc = system_map.get_location("pad1")

        async def _thread_gen(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield m

        thread = ThreadTemplate(
            labware_template=plate, start=pad_loc, end=pad_loc, func=_thread_gen,
        )
        workflow = WorkflowTemplate("test_workflow")
        workflow.add_thread(thread, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="", labwares=[plate],
            resources_registry=registry, system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()

        try:
            submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
            # Wait for the slow action to begin -- entry thread is now running
            # but blocked on ``release_action``. If the IN_PROGRESS transition
            # is gated on entry-thread completion, status would still be
            # ACCEPTED here.
            await asyncio.wait_for(action_started.wait(), timeout=5.0)

            # The transition is event-loop scheduled; one yield is enough for
            # _run_workflow's gate-and-set sequence to run after the entry
            # thread reports it has started.
            snapshot = runtime.submissions.get_submission(submission.id)
            for _ in range(10):
                snapshot = runtime.submissions.get_submission(submission.id)
                if snapshot.status is SubmissionStatus.IN_PROGRESS:
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail(
                    f"Submission status stayed at {snapshot.status.value!r} "
                    "while entry thread was actively running. The "
                    "ACCEPTED -> IN_PROGRESS transition is gated on entry-"
                    "thread completion."
                )

            release_action.set()
            status = await asyncio.wait_for(
                runtime.wait(submission.execution_id), timeout=10.0,
            )
            assert status.status == ExecutionState.COMPLETED

            # After execution completes, submission must reach a terminal state.
            terminal_snap = runtime.submissions.get_submission(submission.id)
            assert terminal_snap.status is SubmissionStatus.COMPLETED, (
                f"Submission status should be COMPLETED after execution "
                f"completed; got {terminal_snap.status.value!r}"
            )
        finally:
            release_action.set()
            await runtime.shutdown()


# ---------------------------------------------------------------------------
# End-to-end ops_history thread_id propagation
#
# Two upstream context-rebuild sites were dropping thread fields. Pinning
# the ACTION.COMPLETED event proved the event-bus path carries thread_id,
# but every real ops_history record still landed with thread_id=''. Pin
# the contract end-to-end
# against the actual store so any third drop site fails this test loud.
# ---------------------------------------------------------------------------


class TestOpsHistoryThreadIdEndToEnd:

    async def test_ops_history_records_carry_thread_id(self) -> None:
        """Run a workflow whose action declares tracking. Every TrackingRecord
        and every nested OperationRecord persisted to ops_history must carry
        the owning thread_id. Empty thread_id breaks the search-by-thread
        filter and makes audit replay impossible."""
        from orca.state.records import DeclaredTracking

        device = UniversalMockDevice("device1")
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_96")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)

        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)

        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

        @orca.action(
            device=pool, inputs=[plate],
            declares=DeclaredTracking(wells_used={"plate_96": ["A1", "A2"]}),
        )
        async def shake_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        async def _m_gen(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shake_action

        m = MethodTemplate("shake_method", func=_m_gen)
        pad_loc = system_map.get_location("pad1")

        async def _thread_gen(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield m

        thread = ThreadTemplate(
            labware_template=plate, start=pad_loc, end=pad_loc, func=_thread_gen,
        )
        workflow = WorkflowTemplate("test_workflow")
        workflow.add_thread(thread, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="", labwares=[plate],
            resources_registry=registry, system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()

        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=30.0)
        assert status.status == ExecutionState.COMPLETED

        records = await runtime.ops_history.list(record.id)
        await runtime.shutdown()

        # Filter the bootstrap initial-state records out; we care about
        # records emitted by real action execution.
        action_records = [r for r in records if r.thread_id != SYSTEM_ID]
        assert action_records, (
            "Expected at least one action-emitted ops_history record; "
            "got only bootstrap records. Did the action's declares ever "
            "reach the observer?"
        )
        for rec in action_records:
            assert rec.thread_id, (
                f"TrackingRecord.thread_id is empty for action_id={rec.action_id}. "
                "Some upstream site is dropping thread fields before they "
                "reach the observer."
            )
            for op in rec.operations:
                assert op.thread_id, (
                    f"OperationRecord.thread_id is empty for action_id="
                    f"{op.action_id}, operation={op.operation.value}. "
                    "search-by-thread filter would be a no-op for this op."
                )


# ---------------------------------------------------------------------------
# Handler-driven mutation during execution
# ---------------------------------------------------------------------------

class TestHandlerDrivenMutation:

    async def test_baseline_two_methods_both_execute(self) -> None:
        f = await _build_system()
        collector = CollectorSink()
        f.runtime.register_sink(collector)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=30.0)
        assert status.status == ExecutionState.COMPLETED

        completed = [e for e in collector.events if e.entity_type == "METHOD" and e.status == "COMPLETED"]
        assert len(completed) == 2
        await f.runtime.shutdown()

    async def test_handler_removes_pending_method(self) -> None:
        """Handler removes seal_method after shake completes. Only shake runs."""
        f = await _build_system()
        collector = CollectorSink()
        f.runtime.register_sink(collector)

        class RemoveHandler(IEventHandler):
            def __init__(self) -> None:
                self.system: ISystem | None = None
                self._pending_thread_id: str | None = None
            def set_system(self, system: ISystem) -> None:
                self.system = system
            def handle(self, event: str, context: ExecutionContext) -> None:
                assert self.system is not None
                if event == "METHOD.COMPLETED" and isinstance(context, MethodExecutionContext):
                    if context.method_name == "shake_method" and context.thread_id is not None:
                        self._pending_thread_id = context.thread_id
                        self.system.pause_thread(context.thread_id)
                elif event == "THREAD.PAUSED" and self._pending_thread_id is not None:
                    if isinstance(context, ThreadExecutionContext) and context.thread_id == self._pending_thread_id:
                        self.system.skip_pending_method(
                            self._pending_thread_id, method_name="seal_method"
                        )
                        self.system.resume_thread(self._pending_thread_id)
                        self._pending_thread_id = None

        handler = RemoveHandler()
        f.event_bus.subscribe("METHOD.COMPLETED", handler)
        f.event_bus.subscribe("THREAD.PAUSED", handler)
        await f.runtime.start()
        handler.set_system(f.runtime.system)

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=30.0)
        assert status.status == ExecutionState.COMPLETED

        thread_snapshots = f.runtime.list_threads(record.id)
        thread = f.runtime.system.get_executing_thread(thread_snapshots[0].id)
        completed_names = [m.name for m in thread.completed_methods]
        assert "shake_method" in completed_names
        assert "seal_method" in completed_names, "Skipped method should be in completed list"
        skipped = [m for m in thread.completed_methods if m.was_skipped]
        assert len(skipped) == 1
        assert skipped[0].name == "seal_method"
        await f.runtime.shutdown()

    async def test_handler_appends_extra_method(self) -> None:
        """Handler appends extra method after shake. Three methods run."""
        f = await _build_system()
        collector = CollectorSink()
        f.runtime.register_sink(collector)

        class AppendHandler(IEventHandler):
            def __init__(self, plate: LabwareTemplate, pool: ResourcePool) -> None:
                self.system: ISystem | None = None
                self._pending_thread_id: str | None = None
                self._plate = plate
                self._pool = pool
            def set_system(self, system: ISystem) -> None:
                self.system = system
            def handle(self, event: str, context: ExecutionContext) -> None:
                assert self.system is not None
                if event == "METHOD.COMPLETED" and isinstance(context, MethodExecutionContext):
                    if context.method_name == "shake_method" and context.thread_id is not None:
                        self._pending_thread_id = context.thread_id
                        self.system.pause_thread(context.thread_id)
                elif event == "THREAD.PAUSED" and self._pending_thread_id is not None:
                    if isinstance(context, ThreadExecutionContext) and context.thread_id == self._pending_thread_id:
                        @orca.action(device=self._pool, inputs=[self._plate])
                        async def extra_act(ctx: ActionContext) -> None:
                            await ctx.device().shake(duration=1, speed=200)

                        async def _extra_gen(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
                            yield extra_act

                        extra = MethodTemplate("extra_method", func=_extra_gen)
                        self.system.insert_method(
                            self._pending_thread_id, extra, where=AtTail()
                        )
                        self.system.resume_thread(self._pending_thread_id)
                        self._pending_thread_id = None

        handler = AppendHandler(f.plate, f.pool)
        f.event_bus.subscribe("METHOD.COMPLETED", handler)
        f.event_bus.subscribe("THREAD.PAUSED", handler)
        await f.runtime.start()
        handler.set_system(f.runtime.system)

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=30.0)
        assert status.status == ExecutionState.COMPLETED

        names = [e.context.method_name for e in collector.events
                 if e.entity_type == "METHOD" and e.status == "COMPLETED"
                 and isinstance(e.context, MethodExecutionContext)]
        assert names == ["shake_method", "seal_method", "extra_method"]
        await f.runtime.shutdown()

    async def test_handler_replaces_pending_method(self) -> None:
        """Handler replaces seal_method with replacement_method."""
        f = await _build_system()
        collector = CollectorSink()
        f.runtime.register_sink(collector)

        class ReplaceHandler(IEventHandler):
            def __init__(self, plate: LabwareTemplate, pool: ResourcePool) -> None:
                self.system: ISystem | None = None
                self._fired = False
                self._plate = plate
                self._pool = pool
                self._pending_thread_id: str | None = None
            def set_system(self, system: ISystem) -> None:
                self.system = system
            def handle(self, event: str, context: ExecutionContext) -> None:
                assert self.system is not None
                if event == "METHOD.COMPLETED" and not self._fired:
                    if not isinstance(context, MethodExecutionContext):
                        return
                    if context.method_name != "shake_method" or context.thread_id is None:
                        return
                    self._fired = True
                    self._pending_thread_id = context.thread_id
                    self.system.pause_thread(context.thread_id)
                elif event == "THREAD.PAUSED" and self._pending_thread_id is not None:
                    if not isinstance(context, ThreadExecutionContext):
                        return
                    if context.thread_id != self._pending_thread_id:
                        return
                    @orca.action(device=self._pool, inputs=[self._plate])
                    async def repl_act(ctx: ActionContext) -> None:
                        await ctx.device().shake(duration=1, speed=999)

                    async def _repl_gen(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
                        yield repl_act

                    replacement = MethodTemplate("replacement_method", func=_repl_gen)
                    # Replace = skip old + insert new at same position
                    self.system.skip_pending_method(self._pending_thread_id, method_name="seal_method")
                    self.system.insert_method(self._pending_thread_id, replacement, where=AtHead())
                    self.system.resume_thread(self._pending_thread_id)
                    self._pending_thread_id = None

        handler = ReplaceHandler(f.plate, f.pool)
        f.event_bus.subscribe("METHOD.COMPLETED", handler)
        f.event_bus.subscribe("THREAD.PAUSED", handler)
        await f.runtime.start()
        handler.set_system(f.runtime.system)

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=30.0)
        assert status.status == ExecutionState.COMPLETED

        thread_snapshots = f.runtime.list_threads(record.id)
        thread = f.runtime.system.get_executing_thread(thread_snapshots[0].id)
        completed_names = [m.name for m in thread.completed_methods]
        assert "replacement_method" in completed_names
        assert "seal_method" in completed_names, "Skipped method should be in completed list"
        skipped = [m for m in thread.completed_methods if m.was_skipped]
        assert len(skipped) == 1
        assert skipped[0].name == "seal_method"
        await f.runtime.shutdown()

    async def test_handler_inserts_method_next(self) -> None:
        """Handler inserts method after shake. Order: shake, inserted, seal."""
        f = await _build_system()
        collector = CollectorSink()
        f.runtime.register_sink(collector)

        class InsertHandler(IEventHandler):
            def __init__(self, plate: LabwareTemplate, pool: ResourcePool) -> None:
                self.system: ISystem | None = None
                self._fired = False
                self._plate = plate
                self._pool = pool
                self._pending_thread_id: str | None = None
            def set_system(self, system: ISystem) -> None:
                self.system = system
            def handle(self, event: str, context: ExecutionContext) -> None:
                assert self.system is not None
                if event == "METHOD.COMPLETED" and not self._fired:
                    if not isinstance(context, MethodExecutionContext):
                        return
                    if context.method_name != "shake_method" or context.thread_id is None:
                        return
                    self._fired = True
                    self._pending_thread_id = context.thread_id
                    self.system.pause_thread(context.thread_id)
                elif event == "THREAD.PAUSED" and self._pending_thread_id is not None:
                    if not isinstance(context, ThreadExecutionContext):
                        return
                    if context.thread_id != self._pending_thread_id:
                        return
                    @orca.action(device=self._pool, inputs=[self._plate])
                    async def ins_act(ctx: ActionContext) -> None:
                        await ctx.device().shake(duration=1, speed=200)

                    async def _ins_gen(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
                        yield ins_act

                    inserted = MethodTemplate("inserted_method", func=_ins_gen)
                    self.system.insert_method(self._pending_thread_id, inserted, where=AtHead())
                    self.system.resume_thread(self._pending_thread_id)
                    self._pending_thread_id = None

        handler = InsertHandler(f.plate, f.pool)
        f.event_bus.subscribe("METHOD.COMPLETED", handler)
        f.event_bus.subscribe("THREAD.PAUSED", handler)
        await f.runtime.start()
        handler.set_system(f.runtime.system)

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=30.0)
        assert status.status == ExecutionState.COMPLETED

        names = [e.context.method_name for e in collector.events
                 if e.entity_type == "METHOD" and e.status == "COMPLETED"
                 and isinstance(e.context, MethodExecutionContext)]
        assert names == ["shake_method", "inserted_method", "seal_method"]
        await f.runtime.shutdown()


# ---------------------------------------------------------------------------
# SMC assay E2E with mutation (slow)
# ---------------------------------------------------------------------------

class TestSmcAssayWithMutation:
    """Comprehensive SMC assay E2E testing all 4 mutation operations.

    A single handler fires at different method completions to test
    append, insert, remove, and replace. Each assertion identifies
    which operation failed.

    All mutations use smc_pro (reader on ddr_3, same corridor as
    bravo_384) to avoid the translator_2 livelock with tips_384.
    """

    @pytest.mark.asyncio
    @pytest.mark.slow
    @pytest.mark.timeout(360)
    async def test_smc_all_mutation_operations(self) -> None:
        from examples.smc_assay.smc_assay_example import build_smc
        smc = await build_smc()
        # SystemBuild.workflow is Optional in the multi-workflow shape;
        # build_smc() always populates it for the SMC example.
        assert smc.workflow is not None, "build_smc() must produce a workflow"

        plate_1_template = smc.system.get_labware_thread_template(
            smc.workflow.name, "plate_1",
        )
        smc_pro = smc.system.get_resource("smc_pro")
        assert isinstance(smc_pro, Device)
        labware = plate_1_template.labware_template

        def make_read_method(name: str) -> MethodTemplate:
            output_file = f"{name}.csv"

            @orca.action(device=smc_pro, inputs=[labware])
            async def read_action(ctx: ActionContext) -> None:
                await ctx.device().read(
                    protocol_filepath="test.prt", output_filepath=output_file
                )

            async def _gen(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
                yield read_action

            return MethodTemplate(name, func=_gen)

        class ComprehensiveMutationHandler(IEventHandler):
            """Uses pause-then-mutate pattern for all 4 operations.

            On METHOD.COMPLETED: stores the pending operation and requests pause.
            On THREAD.PAUSED: executes the stored operation and resumes.

            1. After incubate_2hrs: INSERT 'appended_read' at end
            2. After post_capture_wash: INSERT 'inserted_read' at position 0
            3. After incubate_1hr: SKIP 'incubate_10min'
            4. After post_detection_wash: SKIP 'final_aspiration' + INSERT 'replacement_read'
            """

            def __init__(self, system: ISystem) -> None:
                self.system = system
                self._ops_fired: set[str] = set()
                self._pending_op: tuple[str, str] | None = None  # (op_name, thread_id)

            def set_system(self, system: ISystem) -> None:
                self.system = system

            def handle(self, event: str, context: ExecutionContext) -> None:
                if event == "METHOD.COMPLETED":
                    if not isinstance(context, MethodExecutionContext):
                        return
                    if context.thread_id is None:
                        return
                    name = context.method_name

                    op: str | None = None
                    if name == "incubate_2hrs" and "append" not in self._ops_fired:
                        op = "append"
                    elif name == "post_capture_wash" and "insert" not in self._ops_fired:
                        op = "insert"
                    elif name == "incubate_1hr" and "remove" not in self._ops_fired:
                        op = "remove"
                    elif name == "post_detection_wash" and "replace" not in self._ops_fired:
                        op = "replace"

                    if op is not None:
                        self._ops_fired.add(op)
                        self._pending_op = (op, context.thread_id)
                        self.system.pause_thread(context.thread_id)

                elif event == "THREAD.PAUSED" and self._pending_op is not None:
                    if not isinstance(context, ThreadExecutionContext):
                        return
                    op, tid = self._pending_op
                    if context.thread_id != tid:
                        return
                    self._pending_op = None

                    if op == "append":
                        self.system.insert_method(tid, make_read_method("appended_read"), where=AtTail())
                    elif op == "insert":
                        self.system.insert_method(tid, make_read_method("inserted_read"), where=AtHead())
                    elif op == "remove":
                        self.system.skip_pending_method(tid, method_name="incubate_10min")
                    elif op == "replace":
                        self.system.skip_pending_method(tid, method_name="final_aspiration")
                        self.system.insert_method(tid, make_read_method("replacement_read"), where=AtHead())

                    self.system.resume_thread(tid)

        runtime = SystemRuntime(smc.system, event_bus=smc.event_bus)
        tracker = MethodTracker()
        runtime.register_plugin(tracker)

        handler = ComprehensiveMutationHandler(smc.system)
        smc.event_bus.subscribe("METHOD.COMPLETED", handler)
        smc.event_bus.subscribe("THREAD.PAUSED", handler)

        await runtime.start()
        record = await runtime.submit_workflow(smc.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        await asyncio.wait_for(runtime.wait(record.id), timeout=300.0)
        await runtime.shutdown()

        # Collect plate_1's completed methods
        snapshots = tracker.all_completed_snapshots
        plate_1_methods: list[str] = []
        for tid, name in tracker._thread_names.items():
            if name.startswith("plate_1"):
                plate_1_methods = snapshots.get(tid, [])
                break

        assert len(plate_1_methods) > 0, "plate_1 thread should have completed methods"

        # Verify all 4 operations fired
        assert len(handler._ops_fired) == 4, (
            f"All 4 mutation operations should have fired, but only these did: {handler._ops_fired}"
        )

        # APPEND: appended_read should be the last method
        assert "appended_read" in plate_1_methods, (
            f"APPEND failed: 'appended_read' not in plate_1 methods: {plate_1_methods}"
        )
        assert plate_1_methods[-1] == "appended_read", (
            f"APPEND ordering: 'appended_read' should be last, but order was: {plate_1_methods}"
        )

        # INSERT: inserted_read should appear right after post_capture_wash
        assert "inserted_read" in plate_1_methods, (
            f"INSERT failed: 'inserted_read' not in plate_1 methods: {plate_1_methods}"
        )
        pcw_idx = plate_1_methods.index("post_capture_wash")
        ins_idx = plate_1_methods.index("inserted_read")
        assert ins_idx == pcw_idx + 1, (
            f"INSERT ordering: 'inserted_read' should be right after 'post_capture_wash' "
            f"(index {pcw_idx}), but it's at index {ins_idx}. Order: {plate_1_methods}"
        )

        # SKIP (remove): incubate_10min should be in completed but flagged as skipped
        assert "incubate_10min" in plate_1_methods, (
            f"SKIP failed: 'incubate_10min' should be in completed (as skipped): {plate_1_methods}"
        )

        # REPLACE (skip + insert): replacement_read present, final_aspiration skipped
        assert "replacement_read" in plate_1_methods, (
            f"REPLACE failed: 'replacement_read' not in plate_1 methods: {plate_1_methods}"
        )
        assert "final_aspiration" in plate_1_methods, (
            f"REPLACE failed: 'final_aspiration' should be in completed (as skipped): {plate_1_methods}"
        )


# ---------------------------------------------------------------------------
# Step 2 TDD: Manual pause, pause gate, action mutation
# ---------------------------------------------------------------------------


class TestManualPause:
    """Manual pause/resume API (separate from error-recovery pause)."""

    async def test_pause_thread_pauses_at_next_safe_point(self) -> None:
        """After pause_thread, thread enters PAUSED between actions."""
        f = await _build_system()
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)
        thread_id = threads[0].id

        f.runtime.pause_thread(record.id, thread_id)
        paused_id = await wait_for_paused(f.runtime, record.id)

        assert paused_id == thread_id
        await f.runtime.shutdown()

    async def test_resume_thread_continues_execution(self) -> None:
        """After resume_thread, a manually paused thread continues."""
        f = await _build_system()
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)
        thread_id = threads[0].id

        f.runtime.pause_thread(record.id, thread_id)
        await wait_for_paused(f.runtime, record.id)

        f.runtime.resume_thread(record.id, thread_id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED
        await f.runtime.shutdown()

    async def test_resume_error_paused_thread_raises(self) -> None:
        """resume_thread on an error-paused thread raises ValueError."""
        from tests.test_error_recovery import _build_failing_system
        from orca.workflow_models.status_enums import RecoveryDecision

        f = await _build_failing_system()
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused_id = await wait_for_paused(f.runtime, record.id)

        with pytest.raises(ValueError):
            f.runtime.resume_thread(record.id, paused_id)

        f.runtime.recover_thread(record.id, paused_id, RecoveryDecision.ABORT_THREAD)
        try:
            await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        except Exception:
            pass
        await f.runtime.shutdown()


class TestPauseGateOnMutation:
    """ALL mutation requires thread to be PAUSED."""

    async def test_method_mutation_rejected_when_not_paused(self) -> None:
        """insert_method on a running thread raises ValueError."""
        f = await _build_system()
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)
        thread_id = threads[0].id

        @orca.action(device=f.pool, inputs=[f.plate])
        async def extra_act(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=100)

        @orca.method
        async def extra(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield extra_act

        with pytest.raises(ValueError, match="PAUSED"):
            f.runtime.system.insert_method(thread_id, extra, where=AtTail())

        await f.runtime.abort_execution(record.id)
        await f.runtime.shutdown()


class TestActionMutation:
    """Action-level mutation on ExecutingMethod during pause."""

    async def test_action_append_during_error_pause(self) -> None:
        """Pause on error, append an action, retry. Both original + appended run."""
        from tests.test_error_recovery import _build_two_action_failing_system
        from orca.workflow_models.status_enums import RecoveryDecision

        f = await _build_two_action_failing_system()
        collector = CollectorSink()
        f.runtime.register_sink(collector)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        thread_id = await wait_for_paused(f.runtime, record.id)

        @orca.action(
            device=f.runtime.system.get_resource_pool("shaker1"),
            inputs=[
                f.runtime.system.get_labware_thread_template(
                    f.workflow.name, "plate_96",
                ).labware_template
            ],
        )
        async def extra_seal(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=100, duration=1)

        f.runtime.system.insert_action(thread_id, extra_seal, where=AtTail())

        f.device.should_fail = False
        f.runtime.recover_thread(record.id, thread_id, RecoveryDecision.RETRY)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.COMPLETED

        action_events = [e for e in collector.events
                         if e.entity_type == "ACTION" and e.status == "COMPLETED"]
        assert len(action_events) >= 3, (
            f"Expected at least 3 completed actions (shake retry + seal + appended seal), "
            f"got {len(action_events)}"
        )
        await f.runtime.shutdown()

    async def test_action_remove_during_error_pause(self) -> None:
        """Pause on error, remove the next pending action, skip failed, workflow completes."""
        from tests.test_error_recovery import _build_two_action_failing_system
        from orca.workflow_models.status_enums import RecoveryDecision

        f = await _build_two_action_failing_system()
        collector = CollectorSink()
        f.runtime.register_sink(collector)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        thread_id = await wait_for_paused(f.runtime, record.id)

        f.runtime.system.skip_pending_action(thread_id, action_command="seal")

        f.runtime.recover_thread(record.id, thread_id, RecoveryDecision.ABORT_ACTION)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.COMPLETED

        from orca.events.execution_context import LocationActionExecutionContext
        device_action_completed = [e for e in collector.events
                                   if e.entity_type == "ACTION" and e.status == "COMPLETED"
                                   and isinstance(e.context, LocationActionExecutionContext)]
        assert len(device_action_completed) == 0, (
            f"No device actions should have completed (shake skipped, seal skipped), "
            f"got {len(device_action_completed)}"
        )
        await f.runtime.shutdown()


# ---------------------------------------------------------------------------
# Mutation with variable-parameterized actions
# ---------------------------------------------------------------------------


class RecordingDevice(UniversalMockDevice):
    """Device that records shake call arguments."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.shake_calls: list[tuple[int, int]] = []

    async def shake(self, duration: int, speed: int) -> None:
        self.shake_calls.append((duration, speed))
        await super().shake(duration, speed)


class TestMutationWithVariables:
    """Inserted methods/actions with Var() references resolve correctly."""

    async def test_inserted_method_with_var_resolves(self) -> None:
        """insert_method with a Var-parameterized action resolves from the store."""
        from orca.variables import Var, VariableDefinition

        device = RecordingDevice("device1")
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_96")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)

        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

        # Initial method: literal shake (no variables)
        @orca.action(device=pool, inputs=[plate])
        async def shake1(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.method
        async def first_shake(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shake1

        m1 = first_shake

        pad_loc = system_map.get_location("pad1")

        async def _thread_gen(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield m1

        thread = ThreadTemplate(labware_template=plate, start=pad_loc, end=pad_loc,
                                func=_thread_gen)
        workflow = WorkflowTemplate("mutation_var_workflow")
        workflow.add_variable("inserted_duration", VariableDefinition(type="int", default=4444))
        workflow.add_thread(thread, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test", description="", labwares=[plate],
            resources_registry=registry, system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()

        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(runtime, record.id)
        await pause_and_wait(runtime, record.id, threads[0].id)

        # First shake completed with literal values
        assert len(device.shake_calls) == 1
        assert device.shake_calls[0] == (1, 500)

        # Insert a new method with Var-parameterized shake
        @orca.action(device=pool, inputs=[plate])
        async def var_shake(ctx: ActionContext) -> None:
            duration = await ctx.param("inserted_duration")
            await ctx.device().shake(duration=duration, speed=800)

        @orca.method
        async def var_shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield var_shake

        inserted_method = var_shake_method
        runtime.system.insert_method(threads[0].id, inserted_method, where=AtTail())

        runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED

        # The inserted shake should have resolved inserted_duration=4444
        assert len(device.shake_calls) == 2
        assert device.shake_calls[1] == (4444, 800), (
            f"Expected inserted method to use variable default 4444, "
            f"got duration={device.shake_calls[1][0]}"
        )
        await runtime.shutdown()
