"""TDD tests for MutationCoordinator: skip + insert primitives.

All tests verify PHYSICAL effects (device call counts, plate location)
not just list state. Tests are written FIRST and should FAIL until
the MutationCoordinator, skip flags, and execution logic are implemented.

TDD sequence: write ALL tests -> run -> confirm FAIL -> implement -> run -> confirm PASS
"""

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Sequence

import pytest

from orca.system.mutation.errors import (
    CannotReplaceCurrentMethodError,
    ReplacementSharesTargetNameError,
)
from orca.workflow_models.action_context import ActionContext
from orca.events.event_handler_interface import IEventHandler
from orca.events.execution_context import ExecutionContext, MethodExecutionContext, ThreadExecutionContext
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.labware import LabwareTemplate
from orca.resource_models.location import Location
from orca.runtime.sinks import CollectorSink
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.events import EventBus
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.system.system_interface import ISystem
import orca.orca as orca
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread
from orca.workflow_models.method_template import IMethodTemplate, JoinTemplate
from orca.workflow_models.mutation_position import After, AtHead, AtTail, Before
from orca.workflow_models.status_enums import LabwareThreadStatus, MethodStatus, RecoveryDecision, FailurePolicy
from orca.workflow_models.thread_context import ThreadContext
from orca.runtime.run_modes import WorkflowRunMode
from tests.mock import UniversalMockDevice
from tests.mutation_helpers import wait_for_threads, pause_and_wait, wait_for_paused
from tests.test_helpers import create_test_plate_template, create_test_transporter, wait_for_runtime_condition, wait_until, wire_system_map


def _make_method(name: str, actions: Sequence[ActionTemplate],
                 failure_policy: FailurePolicy | None = None) -> MethodTemplate:
    """Create a MethodTemplate from a name and a list of action templates."""
    captured = list(actions)

    async def _gen(ctx: ThreadContext) -> AsyncGenerator[ActionTemplate, None]:
        for a in captured:
            yield a

    return MethodTemplate(name, func=_gen, failure_policy=failure_policy)


def _make_thread(labware_template: LabwareTemplate, start: Location, end: Location,
                 methods: Sequence[IMethodTemplate]) -> ThreadTemplate:
    """Create a ThreadTemplate from labware, locations, and a list of method templates."""
    captured = list(methods)

    async def _gen(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        for m in captured:
            yield m

    return ThreadTemplate(labware_template, start, end, func=_gen)


# ---------------------------------------------------------------------------
# Tracking device: records every action call with params
# ---------------------------------------------------------------------------

@dataclass
class ActionCall:
    command: str
    params: dict[str, object] = field(default_factory=dict)


class TrackingDevice(UniversalMockDevice):
    """Mock device that records all action calls for physical verification."""

    def __init__(self, name: str, site_names: list[str] | None = None) -> None:
        super().__init__(name, site_names=site_names)
        self.calls: list[ActionCall] = []
        self.should_fail_shake: bool = False

    async def shake(self, duration: int, speed: int) -> None:
        if self.should_fail_shake:
            self.calls.append(ActionCall("shake", {"duration": duration, "speed": speed, "succeeded": False}))
            raise RuntimeError("Simulated shake failure")
        await super().shake(duration, speed)
        self.calls.append(ActionCall("shake", {"duration": duration, "speed": speed, "succeeded": True}))

    async def seal(self, temperature: int, duration: float) -> None:
        await super().seal(temperature, duration)
        self.calls.append(ActionCall("seal", {"temperature": temperature, "duration": duration}))

    async def cover(self, lidded: bool = True) -> None:
        self.calls.append(ActionCall("cover", {"lidded": lidded}))

    @property
    def shake_count(self) -> int:
        return sum(1 for c in self.calls if c.command == "shake" and c.params.get("succeeded", True))

    @property
    def seal_count(self) -> int:
        return sum(1 for c in self.calls if c.command == "seal")

    @property
    def cover_count(self) -> int:
        return sum(1 for c in self.calls if c.command == "cover")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@dataclass
class MutationFixture:
    runtime: SystemRuntime
    workflow: WorkflowTemplate
    device: TrackingDevice
    event_bus: EventBus
    plate: LabwareTemplate
    pool: ResourcePool


async def _build_mutation_system(
    method_names: list[str] | None = None,
    failure_policy: FailurePolicy = FailurePolicy.PAUSE,
) -> MutationFixture:
    """Build a system with TrackingDevice for physical verification.

    Default: 3 methods (shake_1, shake_2, seal_1) to test skip/insert ordering.
    """
    if method_names is None:
        method_names = ["shake_1", "shake_2", "seal_1"]

    device = TrackingDevice("device1")
    transporter = create_test_transporter("robot1", ["device1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)

    pool = ResourcePool("device1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

    methods: list[MethodTemplate] = []
    for name in method_names:
        if name.startswith("seal"):
            @orca.action(device=pool, inputs=[plate], failure_policy=failure_policy)
            async def action(ctx: ActionContext) -> None:
                await ctx.device().seal(temperature=180, duration=3)
        else:
            @orca.action(device=pool, inputs=[plate], failure_policy=failure_policy)
            async def action(ctx: ActionContext) -> None:
                await ctx.device().shake(duration=1, speed=500)
        methods.append(_make_method(name, [action]))

    pad_loc = system_map.get_location("pad1")
    thread = _make_thread(plate, pad_loc, pad_loc, methods)

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
    return MutationFixture(runtime, workflow, device, event_bus, plate, pool)




# ===========================================================================
# SKIP TESTS: verify NO physical interaction
# ===========================================================================

class TestSkipMethod:
    """Skip flags a method so it does not execute. Device must NOT be called."""

    async def test_skip_method_device_not_called(self) -> None:
        """Skip shake_2. Device should only be called for shake_1 and seal_1."""
        f = await _build_mutation_system()
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)

        await pause_and_wait(f.runtime, record.id, threads[0].id)

        # Skip shake_2 by name
        f.runtime.system.skip_pending_method(threads[0].id, method_name="shake_2")

        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        # Physical verification: shake_2 did NOT execute
        assert f.device.shake_count == 1, (
            f"Only shake_1 should have executed. Got {f.device.shake_count} shakes. "
            f"Calls: {f.device.calls}"
        )
        assert f.device.seal_count == 1, (
            f"seal_1 should have executed. Got {f.device.seal_count} seals."
        )

        # Skipped method should be in completed_methods with is_skipped
        thread = f.runtime.system.get_executing_thread(threads[0].id)
        skipped_methods = [m for m in thread.completed_methods if m.was_skipped]
        assert len(skipped_methods) == 1
        assert skipped_methods[0].name == "shake_2"
        await f.runtime.shutdown()

    async def test_skip_all_methods_thread_completes(self) -> None:
        """Skip all pending methods. Thread should complete with no device calls."""
        f = await _build_mutation_system(method_names=["shake_1", "shake_2"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)

        await pause_and_wait(f.runtime, record.id, threads[0].id)

        # Skip the remaining method by name
        thread = f.runtime.system.get_executing_thread(threads[0].id)
        f.runtime.system.skip_pending_method(threads[0].id, method_name="shake_2")

        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        # shake_1 ran before pause, shake_2 was skipped
        assert f.device.shake_count == 1, (
            f"Only shake_1 should have run (shake_2 skipped). Got {f.device.shake_count} shakes."
        )
        skipped = [m for m in thread.completed_methods if m.was_skipped]
        assert len(skipped) == 1
        assert skipped[0].name == "shake_2"
        await f.runtime.shutdown()


# ===========================================================================
# INSERT TESTS: verify physical execution
# ===========================================================================

class TestInsertMethod:
    """Insert adds a new method that physically executes."""

    async def test_insert_method_executes_and_device_called(self) -> None:
        """Insert a shake method at the end. Device should be called for it."""
        f = await _build_mutation_system(method_names=["seal_1"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)

        await pause_and_wait(f.runtime, record.id, threads[0].id)

        @orca.action(device=f.pool, inputs=[f.plate])
        async def inserted_shake_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=2, speed=300)

        extra = _make_method("inserted_shake", [inserted_shake_action])
        f.runtime.system.insert_method(threads[0].id, extra, where=AtTail())

        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        # Physical: inserted shake executed
        assert f.device.shake_count >= 1, (
            f"Inserted shake should have executed. Calls: {f.device.calls}"
        )
        # seal_1 also ran
        assert f.device.seal_count == 1
        await f.runtime.shutdown()

    async def test_insert_method_via_facade_uses_async_path(self) -> None:
        """L5: ThreadFacade.insert_method uses the native-async insert
        path so the daemon event loop is not blocked on the thread-pool
        fallback. End-to-end: facade -> ISystem.insert_method_async ->
        MutationCoordinator._create_method_instance_from_template_async.
        """
        f = await _build_mutation_system(method_names=["seal_1"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)

        await pause_and_wait(f.runtime, record.id, threads[0].id)

        @orca.action(device=f.pool, inputs=[f.plate])
        async def async_inserted_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=2, speed=300)

        extra = _make_method("async_inserted_method", [async_inserted_action])
        await f.runtime.threads.insert_method(
            record.id, threads[0].id, extra, AtTail(),
            reason="L5 async path", confirm=True,
        )

        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        thread = f.runtime.system.get_executing_thread(threads[0].id)
        completed_names = [m.name for m in thread.completed_methods]
        assert "async_inserted_method" in completed_names
        await f.runtime.shutdown()

    async def test_insert_at_head_runs_before_existing_pending(self) -> None:
        """Insert with AtHead runs before existing pending methods."""
        f = await _build_mutation_system(method_names=["shake_1", "seal_1"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)

        await pause_and_wait(f.runtime, record.id, threads[0].id)

        # Insert a seal at head (before existing seal_1)
        @orca.action(device=f.pool, inputs=[f.plate])
        async def urgent_seal_action(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=200, duration=5)

        urgent = _make_method("urgent_seal", [urgent_seal_action])
        f.runtime.system.insert_method(threads[0].id, urgent, where=AtHead())

        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        # Verify ordering: urgent_seal ran before seal_1
        thread = f.runtime.system.get_executing_thread(threads[0].id)
        completed_names = [m.name for m in thread.completed_methods]
        urgent_idx = completed_names.index("urgent_seal")
        seal_idx = completed_names.index("seal_1")
        assert urgent_idx < seal_idx, (
            f"urgent_seal should run before seal_1. Order: {completed_names}"
        )
        await f.runtime.shutdown()

    async def test_insert_method_correct_execution_id(self) -> None:
        """Inserted method's events must appear in CollectorSink (proves execution_id correct)."""
        f = await _build_mutation_system(method_names=["shake_1"])
        collector = CollectorSink()
        f.runtime.register_sink(collector)
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)

        await pause_and_wait(f.runtime, record.id, threads[0].id)

        @orca.action(device=f.pool, inputs=[f.plate])
        async def dynamic_shake_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=100)

        extra = _make_method("dynamic_shake", [dynamic_shake_action])
        f.runtime.system.insert_method(threads[0].id, extra, where=AtTail())

        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        # CollectorSink must have METHOD.COMPLETED for dynamic_shake
        method_completed_names = [
            e.context.method_name for e in collector.events
            if e.entity_type == "METHOD" and e.status == "COMPLETED"
            and isinstance(e.context, MethodExecutionContext)
        ]
        assert "dynamic_shake" in method_completed_names, (
            f"Dynamic method events should reach CollectorSink. "
            f"Completed methods in events: {method_completed_names}"
        )
        await f.runtime.shutdown()


# ===========================================================================
# REPLACE TESTS: skip + insert composition
# ===========================================================================

class TestReplaceComposition:
    """Replace = skip old + insert new at same position."""

    async def test_replace_method_skips_old_inserts_new(self) -> None:
        """Replace shake_2 with a seal. Old shake NOT called, new seal IS called."""
        f = await _build_mutation_system(method_names=["shake_1", "shake_2", "seal_1"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)

        await pause_and_wait(f.runtime, record.id, threads[0].id)

        # Skip shake_2 + insert replacement before remaining methods
        f.runtime.system.skip_pending_method(threads[0].id, method_name="shake_2")
        @orca.action(device=f.pool, inputs=[f.plate])
        async def replacement_seal_action(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=200, duration=5)

        replacement = _make_method("replacement_seal", [replacement_seal_action])
        f.runtime.system.insert_method(threads[0].id, replacement, where=AtHead())

        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        # Physical: shake_1 ran (1 shake), shake_2 skipped (0 extra shakes),
        # replacement_seal ran (2 seals total), seal_1 ran
        assert f.device.shake_count == 1, (
            f"Only shake_1 should have shaken. Got {f.device.shake_count}. Calls: {f.device.calls}"
        )
        assert f.device.seal_count == 2, (
            f"replacement_seal + seal_1 = 2 seals. Got {f.device.seal_count}. Calls: {f.device.calls}"
        )

        # Ordering: replacement_seal ran where shake_2 would have
        thread_obj = f.runtime.system.get_executing_thread(threads[0].id)
        completed_names = [m.name for m in thread_obj.completed_methods]
        repl_idx = completed_names.index("replacement_seal")
        seal_idx = completed_names.index("seal_1")
        assert repl_idx < seal_idx, (
            f"Replacement should run before seal_1. Order: {completed_names}"
        )
        await f.runtime.shutdown()


# ===========================================================================
# GUARD TESTS
# ===========================================================================

class TestMutationGuards:

    async def test_mutate_on_running_thread_raises(self) -> None:
        """Mutation without pause raises ValueError."""
        f = await _build_mutation_system(method_names=["shake_1", "shake_2"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)

        # Thread is running, not paused
        with pytest.raises(ValueError, match="PAUSED"):
            f.runtime.system.skip_pending_method(threads[0].id, method_name="shake_2")

        await f.runtime.abort_execution(record.id)
        await f.runtime.shutdown()

    async def test_mutate_on_completed_thread_raises(self) -> None:
        """Mutation on completed thread raises ValueError."""
        f = await _build_mutation_system(method_names=["shake_1"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        threads = f.runtime.list_threads(record.id)
        with pytest.raises(ValueError, match="cannot accept mutations"):
            f.runtime.system.skip_pending_method(threads[0].id, method_name="shake_1")
        await f.runtime.shutdown()


# ===========================================================================
# ACTION-LEVEL SKIP + INSERT (during error recovery)
# ===========================================================================

class TestActionMutation:
    """Action mutation during error pause. Verify physical effects."""

    async def test_skip_action_device_not_called(self) -> None:
        """Error pause on shake, skip the pending seal action, retry shake.
        Seal should NOT execute."""
        f = await _build_mutation_system(method_names=["two_action_method"])
        # Override: build manually with 2 actions in 1 method
        device = TrackingDevice("device1")
        device.should_fail_shake = True
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_96")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

        @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.PAUSE)
        async def shake(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.action(device=pool, inputs=[plate])
        async def seal(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=180, duration=3)

        method = _make_method("two_actions", [shake, seal])

        pad_loc = system_map.get_location("pad1")
        thread_tmpl = _make_thread(plate, pad_loc, pad_loc, [method])
        workflow = WorkflowTemplate("test_workflow")
        workflow.add_thread(thread_tmpl, is_start=True)

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

        # Wait for error pause (shake fails)
        paused_id = await wait_for_paused(runtime, record.id)

        # Skip the pending seal action
        runtime.system.skip_pending_action(paused_id, action_command="seal")

        # Fix device and retry
        device.should_fail_shake = False
        runtime.recover_thread(record.id, paused_id, RecoveryDecision.RETRY)

        status = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        # Physical: shake executed (after retry), seal did NOT
        assert device.shake_count == 1, f"Shake should have succeeded on retry. Calls: {device.calls}"
        assert device.seal_count == 0, f"Seal was skipped. Should not have executed. Calls: {device.calls}"
        await runtime.shutdown()

    async def test_insert_action_executes_after_retry(self) -> None:
        """Error pause on shake, insert an extra seal action, retry shake.
        Both shake and inserted seal should execute."""
        device = TrackingDevice("device1")
        device.should_fail_shake = True
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_96")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

        @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.PAUSE)
        async def shake(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        method = _make_method("one_action", [shake])

        pad_loc = system_map.get_location("pad1")
        thread_tmpl = _make_thread(plate, pad_loc, pad_loc, [method])
        workflow = WorkflowTemplate("test_workflow")
        workflow.add_thread(thread_tmpl, is_start=True)

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

        paused_id = await wait_for_paused(runtime, record.id)

        # Insert extra seal action
        @orca.action(device=pool, inputs=[plate])
        async def extra_seal(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=200, duration=5)

        runtime.system.insert_action(paused_id, extra_seal, where=AtTail())

        # Fix and retry
        device.should_fail_shake = False
        runtime.recover_thread(record.id, paused_id, RecoveryDecision.RETRY)

        status = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        # Physical: shake succeeded + inserted seal executed
        assert device.shake_count == 1, f"Shake should succeed on retry. Calls: {device.calls}"
        assert device.seal_count == 1, f"Inserted seal should execute. Calls: {device.calls}"
        await runtime.shutdown()

    async def test_insert_action_with_any_labware_template(self) -> None:
        """Insert an action using AnyLabwareTemplate. Labware wiring should use fallback path."""
        from orca.resource_models.labware import AnyLabwareTemplate as AnyLT
        from orca.workflow_models.action_template import ActionTemplate

        device = TrackingDevice("device1")
        device.should_fail_shake = True
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_96")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

        @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.PAUSE)
        async def shake(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        method = _make_method("one_action", [shake])

        pad_loc = system_map.get_location("pad1")
        thread_tmpl = _make_thread(plate, pad_loc, pad_loc, [method])
        workflow = WorkflowTemplate("test_workflow")
        workflow.add_thread(thread_tmpl, is_start=True)

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
        paused_id = await wait_for_paused(runtime, record.id)

        # Insert seal with AnyLabwareTemplate as input (fallback wiring path)
        @orca.action(device=pool, inputs=[AnyLT()], outputs=[AnyLT()])
        async def any_seal(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=200, duration=5)

        runtime.system.insert_action(paused_id, any_seal, where=AtTail())

        device.should_fail_shake = False
        runtime.recover_thread(record.id, paused_id, RecoveryDecision.RETRY)

        status = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        # Both shake (retry) and seal (AnyLabwareTemplate) executed
        assert device.shake_count == 1
        assert device.seal_count == 1, f"Seal with AnyLabwareTemplate should execute. Calls: {device.calls}"
        await runtime.shutdown()


# ===========================================================================
# EDGE CASES
# ===========================================================================

class TestEdgeCases:

    async def test_skip_then_insert_at_same_index(self) -> None:
        """Skip a method and insert a new one at its position. New one runs in its place."""
        f = await _build_mutation_system(method_names=["shake_1", "shake_2", "seal_1"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)

        await pause_and_wait(f.runtime, record.id, threads[0].id)

        f.runtime.system.skip_pending_method(threads[0].id, method_name="shake_2")

        @orca.action(device=f.pool, inputs=[f.plate])
        async def new_seal_action(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=150, duration=2)

        new_seal = _make_method("new_seal", [new_seal_action])
        f.runtime.system.insert_method(threads[0].id, new_seal, where=AtHead())

        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        # Physical: 1 shake (shake_1), 2 seals (new_seal + seal_1)
        assert f.device.shake_count == 1
        assert f.device.seal_count == 2

        # Ordering
        thread_obj = f.runtime.system.get_executing_thread(threads[0].id)
        completed_names = [m.name for m in thread_obj.completed_methods]
        assert "shake_2" in completed_names, "Skipped method should still be in completed list"
        new_seal_idx = completed_names.index("new_seal")
        seal_1_idx = completed_names.index("seal_1")
        assert new_seal_idx < seal_1_idx
        await f.runtime.shutdown()


# ===========================================================================
# STATE COVERAGE: method + action mutation in every relevant state
# ===========================================================================

class TestMethodStateValidation:
    """Verify skip/insert behavior for methods in each state."""

    async def test_skip_pending_method_succeeds(self) -> None:
        """CREATED (pending) method can be skipped."""
        f = await _build_mutation_system(method_names=["shake_1", "shake_2"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)
        await pause_and_wait(f.runtime, record.id, threads[0].id)

        # shake_2 is pending (CREATED), should succeed
        f.runtime.system.skip_pending_method(threads[0].id, method_name="shake_2")

        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED

        thread = f.runtime.system.get_executing_thread(threads[0].id)
        skipped = [m for m in thread.completed_methods if m.was_skipped]
        assert len(skipped) == 1
        assert skipped[0].name == "shake_2"
        assert skipped[0].status == MethodStatus.SKIPPED
        await f.runtime.shutdown()

    async def test_skip_in_progress_method_raises(self) -> None:
        """IN_PROGRESS method cannot be skipped. Operator must abort instead."""
        f = await _build_mutation_system(
            method_names=["shake_1"], failure_policy=FailurePolicy.PAUSE,
        )
        f.device.should_fail_shake = True

        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)

        thread_id = await wait_for_paused(f.runtime, record.id, timeout=10.0)

        with pytest.raises(ValueError, match="already IN_PROGRESS"):
            f.runtime.system.skip_pending_method(thread_id, method_name="shake_1")

        f.runtime.recover_thread(record.id, thread_id, RecoveryDecision.ABORT_THREAD)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        await f.runtime.shutdown()

    async def test_skip_completed_method_raises(self) -> None:
        """COMPLETED method cannot be skipped (it already ran)."""
        f = await _build_mutation_system(method_names=["shake_1", "shake_2"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED

        # All methods completed. Can't skip any.
        threads = f.runtime.list_threads(record.id)
        with pytest.raises(ValueError, match="cannot accept mutations"):
            f.runtime.system.skip_pending_method(threads[0].id, method_name="shake_1")
        await f.runtime.shutdown()

    async def test_mutation_during_error_pause(self) -> None:
        """Thread error-paused: method mutation should work."""
        device = TrackingDevice("device1")
        device.should_fail_shake = True
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_96")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

        @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.PAUSE)
        async def shake(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.action(device=pool, inputs=[plate])
        async def seal(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=180, duration=3)

        m1 = _make_method("shake_method", [shake])
        m2 = _make_method("seal_method", [seal])

        pad_loc = system_map.get_location("pad1")
        thread_tmpl = _make_thread(plate, pad_loc, pad_loc, [m1, m2])
        workflow = WorkflowTemplate("test_workflow")
        workflow.add_thread(thread_tmpl, is_start=True)

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

        paused_id = await wait_for_paused(runtime, record.id)

        # Thread is error-paused. Skip the pending seal_method.
        runtime.system.skip_pending_method(paused_id, method_name="seal_method")

        # Fix and retry shake
        device.should_fail_shake = False
        runtime.recover_thread(record.id, paused_id, RecoveryDecision.RETRY)

        status = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED

        # shake ran (after retry), seal was skipped
        assert device.shake_count == 1
        assert device.seal_count == 0

        thread = runtime.system.get_executing_thread(paused_id)
        skipped = [m for m in thread.completed_methods if m.was_skipped]
        assert len(skipped) == 1
        assert skipped[0].name == "seal_method"
        await runtime.shutdown()


async def _wait_for_thread_status(
    runtime: SystemRuntime,
    target_status: LabwareThreadStatus,
    timeout: float = 5.0,
) -> ExecutingLabwareThread:
    """Wait until any thread reaches the target status. Return it."""
    def _matching_thread() -> ExecutingLabwareThread | None:
        for entry in runtime._executions.values():
            for t in entry.system.executing_threads:
                if t.status == target_status:
                    return t
        return None

    await wait_for_runtime_condition(
        runtime,
        lambda: _matching_thread() is not None,
        timeout=timeout,
        message=f"No thread reached {target_status.name} within timeout",
    )
    thread = _matching_thread()
    assert thread is not None
    return thread


class TestSharedMethodSkip:
    """Shared method skip/abort across threads."""

    async def test_abort_shared_method_interrupts_co_labware_wait(self) -> None:
        """Thread A enters shared method, waits for co-labware. abort() on the
        shared method interrupts the wait. Both threads exit, device not called."""
        # Two-input shared action: one working site per simultaneously-present labware.
        device = TrackingDevice("device1", site_names=["site-1", "site-2"])
        transporter = create_test_transporter("robot1", ["device1", "pad1", "pad2"])
        plate_a = create_test_plate_template("plate_a")
        plate_b = create_test_plate_template("plate_b")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1", "pad2"])

        # Shared action expects BOTH plates
        @orca.action(device=pool, inputs=[plate_a, plate_b])
        async def shared_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=100)
        shared_method_tmpl = _make_method("shared_shake", [shared_action])

        pad1 = system_map.get_location("pad1")
        pad2 = system_map.get_location("pad2")

        thread_a = _make_thread(plate_a, pad1, pad1, [shared_method_tmpl])
        thread_b = _make_thread(plate_b, pad2, pad2, [JoinTemplate()])

        workflow = WorkflowTemplate("shared_method_test")
        workflow.add_thread(thread_a, is_start=True)
        workflow.add_thread(thread_b, is_start=False)
        workflow.register_auto_spawn(thread_b)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="",
            labwares=[plate_a, plate_b],
            resources_registry=registry, system_map=system_map,
            workflows=[workflow],
            event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        await runtime.start()
        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)

        # Thread A enters shared method (triggers spawn of B), moves plate_a
        # to device, then waits for co-labware (plate_b not yet there).
        waiting_thread = await _wait_for_thread_status(
            runtime, LabwareThreadStatus.AWAITING_CO_THREADS, timeout=10.0,
        )

        # Abort the shared method while thread waits for co-labware
        shared_method = waiting_thread.assigned_method
        assert shared_method is not None
        await shared_method.abort()

        # Both threads should complete quickly (not 300s timeout)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED

        assert shared_method.was_aborted
        assert shared_method.status == MethodStatus.PARTIAL_COMPLETE
        assert device.shake_count == 0
        await runtime.shutdown()


class TestReplaceActionOnSharedMethod:
    """Regression: coordinator.py `_build_wired_action` must wire every thread
    CURRENTLY participating in the target method, not just the thread the
    mutation was called on. Pre-fix, replacing a shared two-input action left
    the co-thread's input slot permanently unassigned; the owner's co-labware
    gate requires every declared slot assigned (not just present), so it
    deadlocked in AWAITING_CO_THREADS with no subject to report.
    """

    async def test_replace_shared_action_wires_both_threads(self) -> None:
        """Two threads converge on a shared two-input action that fails and
        error-pauses. Replacing it must wire both threads' labware into the
        substitute so it actually executes, rather than stranding the second
        thread's slot unassigned."""
        device = TrackingDevice("device1", site_names=["site-1", "site-2"])
        device.should_fail_shake = True
        transporter = create_test_transporter("robot1", ["device1", "pad1", "pad2"])
        plate_a = create_test_plate_template("plate_a")
        plate_b = create_test_plate_template("plate_b")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1", "pad2"])

        @orca.action(device=pool, inputs=[plate_a, plate_b], failure_policy=FailurePolicy.PAUSE)
        async def shared_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=100)
        shared_method_tmpl = _make_method("shared_shake", [shared_action])

        pad1 = system_map.get_location("pad1")
        pad2 = system_map.get_location("pad2")
        thread_a = _make_thread(plate_a, pad1, pad1, [shared_method_tmpl])
        thread_b = _make_thread(plate_b, pad2, pad2, [JoinTemplate()])

        workflow = WorkflowTemplate("shared_replace_test")
        workflow.add_thread(thread_a, is_start=True)
        workflow.add_thread(thread_b, is_start=False)
        workflow.register_auto_spawn(thread_b)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="",
            labwares=[plate_a, plate_b],
            resources_registry=registry, system_map=system_map,
            workflows=[workflow],
            event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        await runtime.start()
        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)

        # Both plates converge; the action body raises and both threads pause.
        paused_id = await wait_for_paused(runtime, record.id, timeout=10.0)

        @orca.action(device=pool, inputs=[plate_a, plate_b])
        async def replacement_action(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=180, duration=3)

        runtime.system.replace_action(paused_id, "shared_action", replacement_action)
        runtime.recover_thread(record.id, paused_id, RecoveryDecision.ABORT_ACTION)

        # Pre-fix this hangs: the owner deadlocks in AWAITING_CO_THREADS
        # because the replacement's plate_b slot was never assigned.
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        assert device.seal_count == 1, (
            f"Replacement should execute with both plates wired. Calls: {device.calls}"
        )
        await runtime.shutdown()

    async def test_replace_shared_action_refuses_when_a_participant_is_unwireable(self) -> None:
        """If the replacement declares an input no CURRENT participant can
        supply, the mutation must refuse loudly rather than stage a
        half-wired action that would deadlock later."""
        from orca.system.mutation.errors import MutationLeavesInputUnassignedError

        device = TrackingDevice("device1")
        device.should_fail_shake = True
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate_a = create_test_plate_template("plate_a")
        unrelated_plate = create_test_plate_template("unrelated_plate")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

        @orca.action(device=pool, inputs=[plate_a], failure_policy=FailurePolicy.PAUSE)
        async def solo_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=100)
        method_tmpl = _make_method("solo", [solo_action])

        pad1 = system_map.get_location("pad1")
        thread_a = _make_thread(plate_a, pad1, pad1, [method_tmpl])
        workflow = WorkflowTemplate("unwireable_replace_test")
        workflow.add_thread(thread_a, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="",
            labwares=[plate_a],
            resources_registry=registry, system_map=system_map,
            workflows=[workflow],
            event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        await runtime.start()
        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused_id = await wait_for_paused(runtime, record.id, timeout=10.0)

        @orca.action(device=pool, inputs=[plate_a, unrelated_plate])
        async def replacement_action(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=180, duration=3)

        with pytest.raises(MutationLeavesInputUnassignedError):
            runtime.system.replace_action(paused_id, "solo_action", replacement_action)

        runtime.recover_thread(record.id, paused_id, RecoveryDecision.ABORT_THREAD)
        await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
        await runtime.shutdown()

    async def test_replace_shared_action_refusal_names_wildcard_input_readably(self) -> None:
        """End-to-end companion to the unit-level wildcard-labeling test in
        test_thread_snapshot_waiting_for.py: an unassigned AnyLabwareTemplate
        slot must not surface its internal `$any` token through the real
        replace_action refusal path."""
        from orca.resource_models.labware import AnyLabwareTemplate as AnyLT
        from orca.system.mutation.errors import MutationLeavesInputUnassignedError

        device = TrackingDevice("device1")
        device.should_fail_shake = True
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate_a = create_test_plate_template("plate_a")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

        @orca.action(device=pool, inputs=[plate_a], failure_policy=FailurePolicy.PAUSE)
        async def solo_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=100)
        method_tmpl = _make_method("solo", [solo_action])

        pad1 = system_map.get_location("pad1")
        thread_a = _make_thread(plate_a, pad1, pad1, [method_tmpl])
        workflow = WorkflowTemplate("wildcard_replace_test")
        workflow.add_thread(thread_a, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="",
            labwares=[plate_a],
            resources_registry=registry, system_map=system_map,
            workflows=[workflow],
            event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        await runtime.start()
        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused_id = await wait_for_paused(runtime, record.id, timeout=10.0)

        # plate_a matches directly, leaving the AnyLT() slot untouched.
        @orca.action(device=pool, inputs=[plate_a, AnyLT()])
        async def replacement_action(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=180, duration=3)

        with pytest.raises(MutationLeavesInputUnassignedError) as exc_info:
            runtime.system.replace_action(paused_id, "solo_action", replacement_action)
        assert "$any" not in str(exc_info.value)
        assert exc_info.value.unassigned_names == ["any labware"]

        runtime.recover_thread(record.id, paused_id, RecoveryDecision.ABORT_THREAD)
        await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
        await runtime.shutdown()


# ===========================================================================
# MULTI-THREAD SKIP: independent threads
# ===========================================================================

class TestMultiThreadSkip:
    """Skip works independently across multiple concurrent threads."""

    async def test_skip_independent_threads(self) -> None:
        """Two independent threads, each with two methods. Skip the second method
        on each thread. First methods execute, second methods skipped."""
        # Both threads park a plate at device1 concurrently (thread A pauses
        # while resident); single occupancy needs a site per plate.
        device = TrackingDevice("device1", site_names=["site-1", "site-2"])
        transporter = create_test_transporter("robot1", ["device1", "pad1", "pad2"])
        plate_a = create_test_plate_template("plate_a")
        plate_b = create_test_plate_template("plate_b")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1", "pad2"])

        @orca.action(device=pool, inputs=[plate_a])
        async def shake_a1(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=100)

        @orca.action(device=pool, inputs=[plate_a])
        async def shake_a2(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=100)

        @orca.action(device=pool, inputs=[plate_b])
        async def shake_b1(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=100)

        @orca.action(device=pool, inputs=[plate_b])
        async def shake_b2(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=100)

        m_a1 = _make_method("shake_a1", [shake_a1])
        m_a2 = _make_method("shake_a2", [shake_a2])
        m_b1 = _make_method("shake_b1", [shake_b1])
        m_b2 = _make_method("shake_b2", [shake_b2])

        pad1 = system_map.get_location("pad1")
        pad2 = system_map.get_location("pad2")
        thread_a = _make_thread(plate_a, pad1, pad1, [m_a1, m_a2])
        thread_b = _make_thread(plate_b, pad2, pad2, [m_b1, m_b2])

        workflow = WorkflowTemplate("two_thread_skip_test")
        workflow.add_thread(thread_a, is_start=True)
        workflow.add_thread(thread_b, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="",
            labwares=[plate_a, plate_b],
            resources_registry=registry, system_map=system_map,
            workflows=[workflow],
            event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        await runtime.start()
        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(runtime, record.id)

        await pause_and_wait(runtime, record.id, threads[0].id)
        await pause_and_wait(runtime, record.id, threads[1].id)

        runtime.system.skip_pending_method(threads[0].id, method_name="shake_a2")
        runtime.system.skip_pending_method(threads[1].id, method_name="shake_b2")

        runtime.resume_thread(record.id, threads[0].id)
        runtime.resume_thread(record.id, threads[1].id)

        status = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        thread_a_obj = runtime.system.get_executing_thread(threads[0].id)
        thread_b_obj = runtime.system.get_executing_thread(threads[1].id)
        a_skipped = [m for m in thread_a_obj.completed_methods if m.was_skipped]
        b_skipped = [m for m in thread_b_obj.completed_methods if m.was_skipped]
        assert len(a_skipped) == 1 and a_skipped[0].name == "shake_a2"
        assert len(b_skipped) == 1 and b_skipped[0].name == "shake_b2"

        assert device.shake_count == 2
        await runtime.shutdown()


# ===========================================================================
# mutate_on_next_pause HELPER
# ===========================================================================

class TestMutateOnNextPause:
    """mutate_on_next_pause: handler calls it, system pauses, mutates, resumes."""

    async def test_handler_uses_mutate_on_next_pause_to_insert(self) -> None:
        """Handler fires on METHOD.COMPLETED, uses mutate_on_next_pause to
        insert a method. The inserted method executes."""
        f = await _build_mutation_system(method_names=["shake_1", "seal_1"])

        class InsertOnComplete(IEventHandler):
            def __init__(self, plate: LabwareTemplate, pool: ResourcePool,
                         runtime: SystemRuntime) -> None:
                self._runtime = runtime
                self._plate = plate
                self._pool = pool
                self._fired = False
                self.execution_id = ""
            def set_system(self, system: ISystem) -> None:
                pass
            def handle(self, event: str, context: ExecutionContext) -> None:
                if self._fired or event != "METHOD.COMPLETED":
                    return
                if not isinstance(context, MethodExecutionContext):
                    return
                if context.method_name != "shake_1" or context.thread_id is None:
                    return
                self._fired = True

                @orca.action(device=self._pool, inputs=[self._plate])
                async def helper_shake(ctx: ActionContext) -> None:
                    await ctx.device().shake(duration=1, speed=100)

                extra = _make_method("inserted_via_helper", [helper_shake])
                self._runtime.mutate_on_next_pause(
                    self.execution_id,
                    context.thread_id,
                    lambda sys, tid: sys.insert_method(tid, extra, where=AtHead())
                )

        handler = InsertOnComplete(f.plate, f.pool, f.runtime)
        f.event_bus.subscribe("METHOD.COMPLETED", handler)

        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        handler.execution_id = record.id
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        threads = f.runtime.list_threads(record.id)
        thread = f.runtime.system.get_executing_thread(threads[0].id)
        completed_names = [m.name for m in thread.completed_methods]
        assert "inserted_via_helper" in completed_names, (
            f"Method inserted via mutate_on_next_pause should have executed. "
            f"Completed: {completed_names}"
        )
        # inserted_via_helper should run before seal_1
        ins_idx = completed_names.index("inserted_via_helper")
        seal_idx = completed_names.index("seal_1")
        assert ins_idx < seal_idx
        assert f.device.shake_count >= 2, (
            f"shake_1 + inserted_via_helper = 2 shakes. Got {f.device.shake_count}"
        )
        await f.runtime.shutdown()

    async def test_handler_uses_mutate_on_next_pause_to_skip(self) -> None:
        """Handler fires on METHOD.COMPLETED, uses mutate_on_next_pause to
        skip the next method. The skipped method does NOT execute."""
        f = await _build_mutation_system(method_names=["shake_1", "shake_2", "seal_1"])

        class SkipOnComplete(IEventHandler):
            def __init__(self, runtime: SystemRuntime) -> None:
                self._runtime = runtime
                self._fired = False
                self.execution_id = ""
            def set_system(self, system: ISystem) -> None:
                pass
            def handle(self, event: str, context: ExecutionContext) -> None:
                if self._fired or event != "METHOD.COMPLETED":
                    return
                if not isinstance(context, MethodExecutionContext):
                    return
                if context.method_name != "shake_1" or context.thread_id is None:
                    return
                self._fired = True
                self._runtime.mutate_on_next_pause(
                    self.execution_id,
                    context.thread_id,
                    lambda sys, tid: sys.skip_pending_method(tid, method_name="shake_2")
                )

        handler = SkipOnComplete(f.runtime)
        f.event_bus.subscribe("METHOD.COMPLETED", handler)

        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        handler.execution_id = record.id
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        # shake_1 ran, shake_2 skipped, seal_1 ran
        assert f.device.shake_count == 1
        assert f.device.seal_count == 1
        await f.runtime.shutdown()

    async def test_mutate_on_already_paused_executes_immediately(self) -> None:
        """If thread is already PAUSED, callback fires immediately."""
        f = await _build_mutation_system(method_names=["shake_1", "seal_1"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)

        await pause_and_wait(f.runtime, record.id, threads[0].id)

        # Thread is already paused. mutate_on_next_pause should execute immediately.
        @orca.action(device=f.pool, inputs=[f.plate])
        async def immediate_shake(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=100)

        extra = _make_method("immediate_insert", [immediate_shake])
        f.runtime.mutate_on_next_pause(
            record.id,
            threads[0].id,
            lambda sys, tid: sys.insert_method(tid, extra, where=AtHead())
        )

        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED
        # immediate_insert should have executed (shake_1 + immediate_insert + seal_1 = 2 shakes)
        assert f.device.shake_count >= 2

        thread = f.runtime.system.get_executing_thread(threads[0].id)
        completed_names = [m.name for m in thread.completed_methods]
        assert "immediate_insert" in completed_names, (
            f"Callback should have fired immediately and method should have executed. "
            f"Completed: {completed_names}"
        )
        await f.runtime.shutdown()

    async def test_mutate_on_completed_thread_raises(self) -> None:
        """mutate_on_next_pause on a completed thread raises ValueError."""
        f = await _build_mutation_system(method_names=["shake_1"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED

        threads = f.runtime.list_threads(record.id)
        with pytest.raises(ValueError, match="completed|COMPLETED"):
            f.runtime.mutate_on_next_pause(
                record.id,
                threads[0].id,
                lambda sys, tid: sys.skip_pending_method(tid, method_name="shake_1")
            )
        await f.runtime.shutdown()

    async def test_mutate_on_next_pause_thread_completes_before_pause(self) -> None:
        """When THREAD.COMPLETED reaches the _PauseHandler before THREAD.PAUSED,
        the handler must skip the callback and unsubscribe both subscriptions.

        Drives the real handler via the event bus: a running (non-paused) thread
        installs the handler, then the COMPLETED status event arrives first. The
        bus is synchronous, so emitting it settles the handler deterministically
        regardless of the live thread's own pace."""
        device = TrackingDevice("device1")
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_96")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

        hold_action = asyncio.Event()

        @orca.action(device=pool, inputs=[plate])
        async def slow_shake(ctx):  # type: ignore[no-untyped-def]
            # Parked in EXECUTING_ACTION until the test tears down (abort cancels
            # this wait), so the status poller below catches the thread reliably.
            await hold_action.wait()
            await ctx.device().shake(duration=1, speed=100)

        method = _make_method("only_method", [slow_shake])
        pad_loc = system_map.get_location("pad1")
        thread_tmpl = _make_thread(plate, pad_loc, pad_loc, [method])
        workflow = WorkflowTemplate("test_workflow")
        workflow.add_thread(thread_tmpl, is_start=True)

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

        executing = await _wait_for_thread_status(
            runtime, LabwareThreadStatus.EXECUTING_ACTION, timeout=5.0,
        )
        tid = executing.id

        callback_called = False

        def mutation_cb(sys: ISystem, t: str) -> None:
            nonlocal callback_called
            callback_called = True

        runtime.mutate_on_next_pause(record.id, tid, mutation_cb)
        assert f"THREAD.{tid}.COMPLETED" in event_bus.subscribers

        # COMPLETED reaches the handler before any PAUSED. emit() is synchronous,
        # so the COMPLETED branch runs inline here.
        completed_ctx = ThreadExecutionContext(
            execution_id=record.id,
            workflow_name=workflow.name,
            thread_id=tid,
            thread_name=executing.name,
            template_name=executing.template_name,
        )
        event_bus.emit(f"THREAD.{tid}.COMPLETED", completed_ctx)

        assert not callback_called, (
            "Callback must NOT run when COMPLETED precedes the pause checkpoint"
        )
        assert f"THREAD.{tid}.PAUSED" not in event_bus.subscribers
        assert f"THREAD.{tid}.COMPLETED" not in event_bus.subscribers

        await runtime.abort_execution(record.id)
        await runtime.shutdown()

    async def test_mutate_on_next_pause_error_paused_does_not_resume(self) -> None:
        """Bug #1/#2: mutate_on_next_pause on error-paused thread should execute
        callback but NOT call resume_thread (which would raise for error-paused).
        Thread stays paused for operator to recover."""
        device = TrackingDevice("device1")
        device.should_fail_shake = True
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_96")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

        @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.PAUSE)
        async def shake(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.action(device=pool, inputs=[plate])
        async def seal(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=180, duration=3)
        m1 = _make_method("shake_method", [shake])
        m2 = _make_method("seal_method", [seal])

        pad_loc = system_map.get_location("pad1")
        thread_tmpl = _make_thread(plate, pad_loc, pad_loc, [m1, m2])
        workflow = WorkflowTemplate("test_workflow")
        workflow.add_thread(thread_tmpl, is_start=True)

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
        paused_id = await wait_for_paused(runtime, record.id)

        # Thread is error-paused. mutate_on_next_pause should execute callback
        # but NOT try to resume (resume_from_manual_pause raises on error-paused).
        callback_called = False

        def mutation_cb(sys: ISystem, tid: str) -> None:
            nonlocal callback_called
            callback_called = True
            sys.skip_pending_method(tid, method_name="seal_method")

        # This should NOT raise -- it should execute the callback and leave
        # the thread paused for operator recovery
        runtime.mutate_on_next_pause(record.id, paused_id, mutation_cb)

        assert callback_called, "Callback should execute on error-paused thread"

        # Thread should still be paused (not resumed)
        thread = runtime.system.get_executing_thread(paused_id)
        assert thread.status == LabwareThreadStatus.PAUSED, (
            f"Thread should still be paused after mutate_on_next_pause on error-paused. "
            f"Status: {thread.status}"
        )

        # Now operator recovers
        device.should_fail_shake = False
        runtime.recover_thread(record.id, paused_id, RecoveryDecision.RETRY)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED
        assert device.seal_count == 0, "seal_method was skipped by the callback"
        await runtime.shutdown()


# ===========================================================================
# EDGE CASE TESTS (reviewer findings)
# ===========================================================================

class TestCoordinatorEdgeCases:

    async def test_skip_with_both_id_and_name_raises(self) -> None:
        """Passing both method_id AND method_name raises ValueError."""
        f = await _build_mutation_system(method_names=["shake_1", "shake_2"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)
        await pause_and_wait(f.runtime, record.id, threads[0].id)

        with pytest.raises(ValueError, match="only one"):
            f.runtime.system.skip_pending_method(
                threads[0].id, method_id="fake-id", method_name="shake_2"
            )

        f.runtime.resume_thread(record.id, threads[0].id)
        await f.runtime.abort_execution(record.id)
        await f.runtime.shutdown()

    async def test_mutate_on_next_pause_without_event_bus_raises(self) -> None:
        """mutate_on_next_pause with no event_bus raises RuntimeError."""
        f = await _build_mutation_system(method_names=["shake_1"])
        # Create a runtime WITHOUT event_bus
        runtime_no_bus = SystemRuntime(f.runtime.system)
        await runtime_no_bus.start()
        record = await runtime_no_bus.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        # No event_bus here by design, so poll for registration; events cannot fire.
        await wait_until(
            lambda: bool(runtime_no_bus.list_threads(record.id)), timeout=10.0
        )
        threads = runtime_no_bus.list_threads(record.id)

        # Thread is running, not paused, and no event_bus
        with pytest.raises(RuntimeError, match="event_bus"):
            runtime_no_bus.mutate_on_next_pause(
                record.id,
                threads[0].id,
                lambda sys, tid: sys.skip_pending_method(tid, method_name="shake_1")
            )

        await runtime_no_bus.abort_execution(record.id)
        await runtime_no_bus.shutdown()


# ===========================================================================
# ANCHOR INSERTS: Before(name) / After(name)
# ===========================================================================


class TestBeforeAnchor:
    """Before(anchor_name) inserts fire immediately before the first consumption
    of an item whose name matches anchor_name."""

    async def test_before_method_runs_just_before_anchor(self) -> None:
        """Before('seal_1') inserts a method that runs right before seal_1
        regardless of insertion time."""
        f = await _build_mutation_system(method_names=["shake_1", "shake_2", "seal_1"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)

        await pause_and_wait(f.runtime, record.id, threads[0].id)

        @orca.action(device=f.pool, inputs=[f.plate])
        async def pre_seal_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=111)

        pre_seal = _make_method("pre_seal", [pre_seal_action])
        f.runtime.system.insert_method(threads[0].id, pre_seal, where=Before("seal_1"))

        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        thread_obj = f.runtime.system.get_executing_thread(threads[0].id)
        names = [m.name for m in thread_obj.completed_methods]
        pre_idx = names.index("pre_seal")
        seal_idx = names.index("seal_1")
        assert pre_idx == seal_idx - 1, (
            f"pre_seal should run immediately before seal_1. Order: {names}"
        )
        await f.runtime.shutdown()

    async def test_multiple_before_same_anchor_fifo(self) -> None:
        """Multiple Before inserts for the same anchor fire in FIFO order
        before the anchor."""
        f = await _build_mutation_system(method_names=["shake_1", "seal_1"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)

        await pause_and_wait(f.runtime, record.id, threads[0].id)

        @orca.action(device=f.pool, inputs=[f.plate])
        async def first_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=1)

        @orca.action(device=f.pool, inputs=[f.plate])
        async def second_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=2)

        first = _make_method("first_pre", [first_action])
        second = _make_method("second_pre", [second_action])
        f.runtime.system.insert_method(threads[0].id, first, where=Before("seal_1"))
        f.runtime.system.insert_method(threads[0].id, second, where=Before("seal_1"))

        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        thread_obj = f.runtime.system.get_executing_thread(threads[0].id)
        names = [m.name for m in thread_obj.completed_methods]
        # FIFO: first_pre before second_pre, both before seal_1.
        assert names.index("first_pre") < names.index("second_pre") < names.index("seal_1"), (
            f"Multiple Before same anchor should be FIFO. Order: {names}"
        )
        await f.runtime.shutdown()

    async def test_before_first_match_wins_for_duplicate_names(self) -> None:
        """If two pending generator items share a name, only the first
        occurrence on the consumption stream triggers the Before anchor."""
        # Pause happens after seal_1 completes; two pending shake_1 items remain.
        f = await _build_mutation_system(method_names=["seal_1", "shake_1", "shake_1"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)

        await pause_and_wait(f.runtime, record.id, threads[0].id)

        @orca.action(device=f.pool, inputs=[f.plate])
        async def pre_shake_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=333)

        pre_shake = _make_method("pre_shake", [pre_shake_action])
        f.runtime.system.insert_method(threads[0].id, pre_shake, where=Before("shake_1"))

        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        thread_obj = f.runtime.system.get_executing_thread(threads[0].id)
        names = [m.name for m in thread_obj.completed_methods]
        # pre_shake appears exactly once, and immediately before the FIRST
        # pending shake_1 (the next one on the consumption stream).
        assert names.count("pre_shake") == 1
        # Find the first shake_1 that appears AFTER pre_shake.
        pre_idx = names.index("pre_shake")
        assert pre_idx >= 0
        # The item right after pre_shake must be a shake_1 (the first match).
        assert names[pre_idx + 1] == "shake_1", (
            f"pre_shake should be immediately followed by the first matching "
            f"shake_1. Order: {names}"
        )
        # And the SECOND shake_1 must come later without any pre_shake before it.
        second_shake_idx = names.index("shake_1", pre_idx + 2)
        assert "pre_shake" not in names[pre_idx + 2:second_shake_idx], (
            f"Second shake_1 should not re-trigger the Before anchor. Order: {names}"
        )
        await f.runtime.shutdown()

class TestAfterAnchor:
    """After(anchor_name) inserts fire immediately after the first consumption
    of an item whose name matches anchor_name completes."""

    async def test_after_method_runs_just_after_anchor(self) -> None:
        f = await _build_mutation_system(method_names=["shake_1", "shake_2", "seal_1"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)

        await pause_and_wait(f.runtime, record.id, threads[0].id)

        @orca.action(device=f.pool, inputs=[f.plate])
        async def post_shake_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=222)

        post_shake = _make_method("post_shake", [post_shake_action])
        f.runtime.system.insert_method(
            threads[0].id, post_shake, where=After("shake_2"),
        )

        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        thread_obj = f.runtime.system.get_executing_thread(threads[0].id)
        names = [m.name for m in thread_obj.completed_methods]
        shake2_idx = names.index("shake_2")
        post_idx = names.index("post_shake")
        assert post_idx == shake2_idx + 1, (
            f"post_shake should run immediately after shake_2. Order: {names}"
        )
        await f.runtime.shutdown()

    async def test_multiple_after_same_anchor_fifo(self) -> None:
        """Multiple After inserts for the same anchor fire in FIFO order
        after the anchor completes."""
        # Pause after seal_1; shake_1 is still pending so After('shake_1')
        # will fire on a future consumption.
        f = await _build_mutation_system(method_names=["seal_1", "shake_1"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)

        await pause_and_wait(f.runtime, record.id, threads[0].id)

        @orca.action(device=f.pool, inputs=[f.plate])
        async def first_post_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=1)

        @orca.action(device=f.pool, inputs=[f.plate])
        async def second_post_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=2)

        first = _make_method("first_post", [first_post_action])
        second = _make_method("second_post", [second_post_action])
        f.runtime.system.insert_method(threads[0].id, first, where=After("shake_1"))
        f.runtime.system.insert_method(threads[0].id, second, where=After("shake_1"))

        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        thread_obj = f.runtime.system.get_executing_thread(threads[0].id)
        names = [m.name for m in thread_obj.completed_methods]
        # FIFO: shake_1 -> first_post -> second_post.
        shake_idx = names.index("shake_1")
        first_idx = names.index("first_post")
        second_idx = names.index("second_post")
        assert shake_idx < first_idx < second_idx, (
            f"Multiple After same anchor should be FIFO. Order: {names}"
        )
        # first_post must immediately follow shake_1.
        assert first_idx == shake_idx + 1
        # second_post must immediately follow first_post.
        assert second_idx == first_idx + 1
        await f.runtime.shutdown()

    async def test_after_first_match_wins_for_duplicate_names(self) -> None:
        """Only the first occurrence of the anchor name on the consumption
        stream (after the insert) triggers the After anchor."""
        # Pause after seal_1 completes; both shake_1 items are still pending.
        f = await _build_mutation_system(method_names=["seal_1", "shake_1", "shake_1"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)

        await pause_and_wait(f.runtime, record.id, threads[0].id)

        @orca.action(device=f.pool, inputs=[f.plate])
        async def post_shake_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=777)

        post_shake = _make_method("post_shake", [post_shake_action])
        f.runtime.system.insert_method(
            threads[0].id, post_shake, where=After("shake_1"),
        )

        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        thread_obj = f.runtime.system.get_executing_thread(threads[0].id)
        names = [m.name for m in thread_obj.completed_methods]
        # post_shake fires exactly once.
        assert names.count("post_shake") == 1
        post_idx = names.index("post_shake")
        # The item IMMEDIATELY BEFORE post_shake must be a shake_1
        # (the first match on the stream).
        assert names[post_idx - 1] == "shake_1", (
            f"post_shake should immediately follow the first matching shake_1. "
            f"Order: {names}"
        )
        # A second shake_1 must exist AFTER post_shake with no post_shake
        # re-fired for it.
        second_shake_idx = names.index("shake_1", post_idx + 1)
        assert "post_shake" not in names[post_idx + 1:second_shake_idx + 1], (
            f"Second shake_1 should not re-trigger the After anchor. Order: {names}"
        )
        await f.runtime.shutdown()


class TestActionAnchorMatchesTagOnly:
    """Action anchors match ActionTemplate.tag only. Untagged actions with
    a matching command name are NOT anchor targets."""

    async def test_before_action_matches_tag_but_not_command(self) -> None:
        """Insert Before('tag_x') with two methods: one containing an action
        tagged 'tag_x' (anchors fire), another containing an action whose
        command is 'tag_x' but with no tag (anchors do NOT fire)."""
        device = TrackingDevice("device1")
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_96")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

        # Action whose function name (and thus `command`) is "tag_x" but no tag.
        @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.PAUSE)
        async def tag_x(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        pad_loc = system_map.get_location("pad1")
        method = _make_method("only_method", [tag_x])
        thread_tmpl = _make_thread(plate, pad_loc, pad_loc, [method])
        workflow = WorkflowTemplate("test_workflow")
        workflow.add_thread(thread_tmpl, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="", labwares=[plate],
            resources_registry=registry, system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        # Make the first shake fail so we can pause between actions.
        device.should_fail_shake = True

        await runtime.start()
        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused_id = await wait_for_paused(runtime, record.id)

        # Insert Before('tag_x') -- since the only pending action's command is
        # 'tag_x' but it has no tag, the anchor should NOT fire.
        @orca.action(device=pool, inputs=[plate])
        async def never_fires(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=100, duration=1)

        runtime.system.insert_action(
            paused_id, never_fires, where=Before("tag_x"),
        )

        # Fix device, retry shake. The pending Before('tag_x') insert must
        # stay unresolved because tag_x has no tag.
        device.should_fail_shake = False
        runtime.recover_thread(record.id, paused_id, RecoveryDecision.RETRY)

        status = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        # The never_fires action should NOT have executed (seal_count = 0).
        assert device.seal_count == 0, (
            f"Before('tag_x') should not match an untagged action whose command "
            f"is 'tag_x'. But never_fires executed. Calls: {device.calls}"
        )
        await runtime.shutdown()

    async def test_before_action_matches_tagged_action(self) -> None:
        """When the method contains an action with tag='tag_x', a Before('tag_x')
        insert fires immediately before that action runs."""
        device = TrackingDevice("device1")
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_96")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

        # First action: shake (fails to trigger pause).
        @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.PAUSE)
        async def shake_fail(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        # Second action: seal, TAGGED as 'target_tag'.
        @orca.action(device=pool, inputs=[plate], tag="target_tag")
        async def tagged_seal(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=180, duration=3)

        pad_loc = system_map.get_location("pad1")
        method = _make_method("two_actions", [shake_fail, tagged_seal])
        thread_tmpl = _make_thread(plate, pad_loc, pad_loc, [method])
        workflow = WorkflowTemplate("test_workflow")
        workflow.add_thread(thread_tmpl, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="", labwares=[plate],
            resources_registry=registry, system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        device.should_fail_shake = True

        await runtime.start()
        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused_id = await wait_for_paused(runtime, record.id)

        # Insert Before('target_tag') -- should fire before the tagged_seal runs.
        @orca.action(device=pool, inputs=[plate])
        async def pre_tag_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=100)

        runtime.system.insert_action(
            paused_id, pre_tag_action, where=Before("target_tag"),
        )

        device.should_fail_shake = False
        runtime.recover_thread(record.id, paused_id, RecoveryDecision.RETRY)

        status = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        # Extract command order from the call log. pre_tag_action and
        # tagged_seal both fire; pre_tag_action must come before tagged_seal.
        commands = [c.command for c in device.calls if c.command in ("shake", "seal")
                    and c.params.get("succeeded", True)]
        # Successful shakes: 1 retry shake + 1 pre_tag shake; seal after.
        assert device.seal_count == 1
        shake_before_seal = any(
            c.command == "shake" and c.params.get("succeeded", True)
            for c in device.calls
        )
        assert shake_before_seal, (
            f"pre_tag_action (shake) should have executed. Calls: {device.calls}"
        )
        # The last non-failing shake (inserted pre_tag_action) must be before
        # the seal call.
        shake_indices = [i for i, c in enumerate(device.calls)
                         if c.command == "shake" and c.params.get("succeeded", True)]
        seal_indices = [i for i, c in enumerate(device.calls) if c.command == "seal"]
        assert shake_indices[-1] < seal_indices[0], (
            f"Inserted pre_tag_action should run before tagged_seal. Calls: {device.calls}"
        )
        await runtime.shutdown()


class TestReplaceOnErroredItem:
    """Characterize the 'replace the errored item' recovery pattern.

    The mutation subsystem (insert/skip) and the recovery-decision subsystem
    (recover_thread) are independent. Existing tests cover insert-on-error-pause
    only paired with RETRY. These capture the actual behavior of the operator
    'replace' move on the *failed* item: insert a substitute, then drop the
    failed item with a recovery decision so the substitute runs in its place.
    The device is left in its failing state throughout -- the point is that the
    replacement, not a retry, carries the thread forward.
    """

    async def test_replace_errored_action_insert_at_head_then_abort_action(self) -> None:
        """shake fails -> insert a seal AtHead into the current method ->
        ABORT_ACTION drops the failed shake. Expect: replacement seal runs in
        the failed shake's place, then the method's original seal runs."""
        device = TrackingDevice("device1")
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_96")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

        @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.PAUSE)
        async def shake_fail(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.action(device=pool, inputs=[plate])
        async def original_seal(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=180, duration=3)

        pad_loc = system_map.get_location("pad1")
        method = _make_method("two_actions", [shake_fail, original_seal])
        thread_tmpl = _make_thread(plate, pad_loc, pad_loc, [method])
        workflow = WorkflowTemplate("test_workflow")
        workflow.add_thread(thread_tmpl, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="", labwares=[plate],
            resources_registry=registry, system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        device.should_fail_shake = True
        await runtime.start()
        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused_id = await wait_for_paused(runtime, record.id)

        @orca.action(device=pool, inputs=[plate])
        async def replacement_seal(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=200, duration=5)

        runtime.system.insert_action(paused_id, replacement_seal, where=AtHead())
        runtime.recover_thread(record.id, paused_id, RecoveryDecision.ABORT_ACTION)

        status = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        # Device never had its failure cleared, so no shake ever succeeded.
        assert device.shake_count == 0, f"No shake should succeed. Calls: {device.calls}"
        # Replacement seal (in the failed shake's place) + original seal = 2.
        assert device.seal_count == 2, (
            f"Replacement seal + original seal expected. Got {device.seal_count}. "
            f"Calls: {device.calls}"
        )
        seal_temps = [c.params.get("temperature") for c in device.calls if c.command == "seal"]
        assert seal_temps == [200, 180], (
            f"Replacement (temp=200) should run before original (temp=180). "
            f"Got {seal_temps}. Calls: {device.calls}"
        )
        await runtime.shutdown()

    async def test_replace_errored_method_insert_at_head_then_abort_method(self) -> None:
        """shake_1 fails -> insert a replacement method AtHead -> ABORT_METHOD
        drops the failed method. Expect: replacement method runs in its place,
        then the downstream seal_1 method runs."""
        f = await _build_mutation_system(method_names=["shake_1", "seal_1"])
        f.device.should_fail_shake = True
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused_id = await wait_for_paused(f.runtime, record.id)

        @orca.action(device=f.pool, inputs=[f.plate])
        async def replacement_seal_action(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=200, duration=5)

        replacement = _make_method("replacement_method", [replacement_seal_action])
        f.runtime.system.insert_method(paused_id, replacement, where=AtHead())
        f.runtime.recover_thread(record.id, paused_id, RecoveryDecision.ABORT_METHOD)

        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        assert f.device.shake_count == 0, (
            f"No shake should succeed. Calls: {f.device.calls}"
        )
        assert f.device.seal_count == 2, (
            f"Replacement method seal + seal_1 expected. Got {f.device.seal_count}. "
            f"Calls: {f.device.calls}"
        )
        thread_obj = f.runtime.system.get_executing_thread(paused_id)
        completed_names = [m.name for m in thread_obj.completed_methods]
        assert "replacement_method" in completed_names, (
            f"Replacement method should have completed. Got {completed_names}"
        )
        assert completed_names.index("replacement_method") < completed_names.index("seal_1"), (
            f"Replacement should run before seal_1. Order: {completed_names}"
        )
        await f.runtime.shutdown()


class TestReplacePending:
    """replace_method / replace_action. PENDING target -> spliced in place
    (returns False). IN_PROGRESS/errored target -> replacement staged to run
    next (returns True); caller drops the failed step via recover_thread."""

    async def test_replace_method_pending_splices_and_returns_false(self) -> None:
        """replace_method('shake_2', seal): shake_2 dropped, replacement seal
        runs in its place before seal_1. Returns False (fully spliced)."""
        f = await _build_mutation_system(method_names=["shake_1", "shake_2", "seal_1"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)
        await pause_and_wait(f.runtime, record.id, threads[0].id)

        @orca.action(device=f.pool, inputs=[f.plate])
        async def replacement_seal_action(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=200, duration=5)

        replacement = _make_method("replacement_seal", [replacement_seal_action])
        staged = await f.runtime.system.replace_method(threads[0].id, "shake_2", replacement)
        assert staged is False, "pending replace should be fully spliced, not staged"

        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        assert f.device.shake_count == 1, (
            f"Only shake_1 should shake (shake_2 replaced). Got {f.device.shake_count}. "
            f"Calls: {f.device.calls}"
        )
        assert f.device.seal_count == 2, (
            f"Replacement seal + seal_1 expected. Got {f.device.seal_count}. "
            f"Calls: {f.device.calls}"
        )
        thread_obj = f.runtime.system.get_executing_thread(threads[0].id)
        completed_names = [m.name for m in thread_obj.completed_methods]
        assert "replacement_seal" in completed_names
        assert completed_names.index("replacement_seal") < completed_names.index("seal_1")
        assert any(m.name == "shake_2" and m.was_skipped for m in thread_obj.completed_methods)
        await f.runtime.shutdown()

    async def test_replace_method_on_errored_stages_then_recover(self) -> None:
        """Replacing the errored in-progress method returns True (staged); the
        replacement runs only after recover_thread(ABORT_METHOD) drops it."""
        f = await _build_mutation_system(method_names=["shake_1", "seal_1"])
        f.device.should_fail_shake = True
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused_id = await wait_for_paused(f.runtime, record.id)

        @orca.action(device=f.pool, inputs=[f.plate])
        async def replacement_seal_action(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=200, duration=5)

        replacement = _make_method("replacement_seal", [replacement_seal_action])
        staged = await f.runtime.system.replace_method(paused_id, "shake_1", replacement)
        assert staged is True, "replacing the errored method should stage, not splice"

        # Still parked: nothing ran yet. The recovery decision is the continue.
        assert f.device.seal_count == 0

        f.runtime.recover_thread(record.id, paused_id, RecoveryDecision.ABORT_METHOD)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        assert f.device.shake_count == 0, f"shake_1 failed + dropped. Calls: {f.device.calls}"
        assert f.device.seal_count == 2, (
            f"Replacement seal + seal_1 expected. Got {f.device.seal_count}. "
            f"Calls: {f.device.calls}"
        )
        thread_obj = f.runtime.system.get_executing_thread(paused_id)
        completed_names = [m.name for m in thread_obj.completed_methods]
        assert "replacement_seal" in completed_names
        assert completed_names.index("replacement_seal") < completed_names.index("seal_1")
        await f.runtime.shutdown()

    async def test_replace_action_pending_splices_and_returns_false(self) -> None:
        """replace_action('seal', replacement) on a pending action: original
        seal dropped, replacement runs next. Returns False."""
        device, runtime, record, paused_id, pool, plate = await self._two_action_errored(
            failing_first=True,
        )

        @orca.action(device=pool, inputs=[plate])
        async def replacement_seal(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=200, duration=5)

        # 'seal' is the PENDING second action; 'shake' is the errored current one.
        staged = runtime.system.replace_action(paused_id, "seal", replacement_seal)
        assert staged is False, "replacing a pending action should splice, not stage"

        device.should_fail_shake = False
        runtime.recover_thread(record.id, paused_id, RecoveryDecision.RETRY)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        assert device.shake_count == 1, f"Shake should succeed on retry. Calls: {device.calls}"
        seal_temps = [c.params.get("temperature") for c in device.calls if c.command == "seal"]
        assert seal_temps == [200], (
            f"Replacement (200) runs, original (180) skipped. Got {seal_temps}. "
            f"Calls: {device.calls}"
        )
        await runtime.shutdown()

    async def test_replace_action_on_errored_stages_then_recover(self) -> None:
        """Replacing the errored current action returns True (staged); the
        replacement runs only after recover_thread(ABORT_ACTION)."""
        device, runtime, record, paused_id, pool, plate = await self._two_action_errored(
            failing_first=True,
        )

        @orca.action(device=pool, inputs=[plate])
        async def replacement_seal(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=300, duration=1)

        # 'shake' is the errored current action.
        staged = runtime.system.replace_action(paused_id, "shake", replacement_seal)
        assert staged is True, "replacing the errored action should stage"

        # Device left failing: proves the replacement, not a retry, carries on.
        runtime.recover_thread(record.id, paused_id, RecoveryDecision.ABORT_ACTION)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        assert device.shake_count == 0, f"No shake should succeed. Calls: {device.calls}"
        seal_temps = [c.params.get("temperature") for c in device.calls if c.command == "seal"]
        # Staged replacement (300) runs in the failed shake's place, then the
        # original pending seal (180).
        assert seal_temps == [300, 180], (
            f"Staged replacement then original seal. Got {seal_temps}. Calls: {device.calls}"
        )
        await runtime.shutdown()

    async def _two_action_errored(
        self, failing_first: bool,
    ) -> tuple[TrackingDevice, SystemRuntime, object, str, ResourcePool, LabwareTemplate]:
        """Build [shake(fails), seal] on one method, run to the error-pause on
        shake. Returns (device, runtime, record, paused_thread_id, pool, plate)."""
        device = TrackingDevice("device1")
        device.should_fail_shake = failing_first
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_96")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

        @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.PAUSE)
        async def shake(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.action(device=pool, inputs=[plate])
        async def seal(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=180, duration=3)

        method = _make_method("two_actions", [shake, seal])
        pad_loc = system_map.get_location("pad1")
        thread_tmpl = _make_thread(plate, pad_loc, pad_loc, [method])
        workflow = WorkflowTemplate("test_workflow")
        workflow.add_thread(thread_tmpl, is_start=True)

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
        paused_id = await wait_for_paused(runtime, record.id)
        return device, runtime, record, paused_id, pool, plate

    async def test_replace_method_refuses_current_method_on_manual_pause(self) -> None:
        """C1 regression: a MANUALLY-paused thread whose current method is the
        target must NOT stage -- the recover_thread guidance it would hand back
        raises on a manual pause. It is refused instead."""
        f = await _build_mutation_system(method_names=["shake_1", "shake_2", "seal_1"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)
        await pause_and_wait(f.runtime, record.id, threads[0].id)

        thread_obj = f.runtime.system.get_executing_thread(threads[0].id)
        current = thread_obj.assigned_method
        assert current is not None, "expected an assigned method at the manual-pause boundary"
        assert not thread_obj.is_error_paused, "thread should be manually paused, not error-paused"

        @orca.action(device=f.pool, inputs=[f.plate])
        async def replacement_seal_action(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=200, duration=5)

        replacement = _make_method("replacement_seal", [replacement_seal_action])
        with pytest.raises(CannotReplaceCurrentMethodError):
            await f.runtime.system.replace_method(threads[0].id, current.name, replacement)

        # No staging happened: resume runs the thread to completion normally.
        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED
        await f.runtime.shutdown()

    async def test_replace_pending_action_runs_at_head_ahead_of_other_pending(self) -> None:
        """L2: with pending actions [seal_a, cover_target], replacing cover puts
        the substitute AtHead -- it runs ahead of seal_a, and cover is skipped."""
        device = TrackingDevice("device1")
        device.should_fail_shake = True
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_96")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

        @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.PAUSE)
        async def shake_fail(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.action(device=pool, inputs=[plate])
        async def seal_a(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=180, duration=3)

        @orca.action(device=pool, inputs=[plate])
        async def cover_target(ctx: ActionContext) -> None:
            await ctx.device().cover(lidded=True)

        method = _make_method("three_actions", [shake_fail, seal_a, cover_target])
        pad_loc = system_map.get_location("pad1")
        thread_tmpl = _make_thread(plate, pad_loc, pad_loc, [method])
        workflow = WorkflowTemplate("test_workflow")
        workflow.add_thread(thread_tmpl, is_start=True)

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
        paused_id = await wait_for_paused(runtime, record.id)

        @orca.action(device=pool, inputs=[plate])
        async def replacement_seal(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=999, duration=1)

        # cover_target is a PENDING action (further along than pending seal_a).
        # The lane keys on the action's command == function name, not the
        # device method it calls.
        staged = runtime.system.replace_action(paused_id, "cover_target", replacement_seal)
        assert staged is False

        device.should_fail_shake = False
        runtime.recover_thread(record.id, paused_id, RecoveryDecision.RETRY)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        assert device.shake_count == 1, f"shake retried once. Calls: {device.calls}"
        assert device.cover_count == 0, f"cover (target) should be skipped. Calls: {device.calls}"
        seal_temps = [c.params.get("temperature") for c in device.calls if c.command == "seal"]
        assert seal_temps == [999, 180], (
            f"AtHead replacement (999) runs ahead of pending seal_a (180). "
            f"Got {seal_temps}. Calls: {device.calls}"
        )
        await runtime.shutdown()

    async def test_replace_method_through_dangerous_facade(self) -> None:
        """L3: drive the real @dangerous ThreadFacade end to end (confirm +
        reason), not just the coordinator -- stages the errored method and the
        recovery decision runs the substitute."""
        f = await _build_mutation_system(method_names=["shake_1", "seal_1"])
        f.device.should_fail_shake = True
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused_id = await wait_for_paused(f.runtime, record.id)

        @orca.action(device=f.pool, inputs=[f.plate])
        async def replacement_seal_action(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=200, duration=5)

        replacement = _make_method("replacement_seal", [replacement_seal_action])
        staged = await f.runtime.threads.replace_method(
            record.id, paused_id, "shake_1", replacement,
            reason="e2e facade", confirm=True,
        )
        assert staged is True

        f.runtime.recover_thread(record.id, paused_id, RecoveryDecision.ABORT_METHOD)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED
        assert f.device.seal_count == 2, f"replacement + seal_1. Calls: {f.device.calls}"
        await f.runtime.shutdown()

    async def test_replace_method_rejects_same_name_replacement(self) -> None:
        """A pending replacement that shares the target's name is refused -- the
        one-shot skip would otherwise drop the replacement, not the original."""
        f = await _build_mutation_system(method_names=["shake_1", "shake_2", "seal_1"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)
        await pause_and_wait(f.runtime, record.id, threads[0].id)

        @orca.action(device=f.pool, inputs=[f.plate])
        async def repl(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=1, duration=1)

        same_named = _make_method("shake_2", [repl])  # same name as the target
        with pytest.raises(ReplacementSharesTargetNameError):
            await f.runtime.system.replace_method(threads[0].id, "shake_2", same_named)

        f.runtime.resume_thread(record.id, threads[0].id)
        await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        await f.runtime.shutdown()

    async def test_replace_action_rejects_same_command_replacement(self) -> None:
        """A pending replacement whose command equals the target is refused."""
        device, runtime, record, paused_id, pool, plate = await self._two_action_errored(
            failing_first=True,
        )

        @orca.action(device=pool, inputs=[plate])
        async def seal(ctx: ActionContext) -> None:  # command "seal" == target
            await ctx.device().seal(temperature=1, duration=1)

        with pytest.raises(ReplacementSharesTargetNameError):
            runtime.system.replace_action(paused_id, "seal", seal)

        runtime.recover_thread(record.id, paused_id, RecoveryDecision.ABORT_THREAD)
        try:
            await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
        except Exception:
            pass
        await runtime.shutdown()

