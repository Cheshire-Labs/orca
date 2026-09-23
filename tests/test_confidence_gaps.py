"""Tests for confidence gaps in the reactive spawn + SDK cleanup work.

Covers moderate and low confidence items:
1. Auto-spawn + error recovery (retry/skip dedup)
2. Recursive auto-spawn (depth > 1)
3. SpawnNewOnFourthPlate chaining with 5+ spawns
4. Multi-workflow auto-spawn isolation
5. ctx.wait_for() in ActionContext
6. Action tag field
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.events.event_bus import EventBus
from orca.events.event_channel import EventChannelRegistry
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.system_runtime import SystemRuntime
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.sdk.system import SystemMap
from orca.system.resource_registry import ResourceRegistry
from orca.variables.variable_store import NullVariableResolver
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.workflows.workflow_factories import MethodActionFactory
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.status_enums import FailurePolicy, RecoveryDecision
from orca.runtime.run_modes import WorkflowRunMode
from tests.mock import UniversalMockDevice
from tests.test_helpers import (
    execution_outcome,
    wire_system_map,
    create_test_plate_template,
    create_test_transporter,
    wait_for_paused_threads,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FailOnceDevice(UniversalMockDevice):
    def __init__(self, name: str, site_names: list[str] | None = None) -> None:
        super().__init__(name, site_names=site_names)
        self.should_fail = True

    async def shake(self, duration: int, speed: int) -> None:
        if self.should_fail:
            raise RuntimeError("Simulated shake failure")
        await super().shake(duration, speed)


async def _build_auto_spawn_system(
    device: UniversalMockDevice | None = None,
    failure_policy: FailurePolicy = FailurePolicy.PAUSE,
):
    """Build a system with auto-spawn: parent action needs child labware."""
    dev = device or FailOnceDevice("shaker1", site_names=["site-1", "site-2"])
    transporter = create_test_transporter("robot1", ["shaker1", "pad1", "pad2"])
    plate_main = create_test_plate_template("plate_main")
    plate_child = create_test_plate_template("plate_child")

    registry = ResourceRegistry()
    registry.add_resource(dev)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [dev])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": dev}, pads=["pad1", "pad2"])

    @orca.action(device=pool, inputs=[plate_main, plate_child], failure_policy=failure_policy)
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.action(device=pool, inputs=[plate_main])
    async def seal_action(ctx: ActionContext) -> None:
        await ctx.device().seal(temperature=180, duration=3)

    @orca.method
    async def parent_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_action
        yield seal_action

    pad1 = system_map.get_location("pad1")
    pad2 = system_map.get_location("pad2")

    @orca.thread(labware=plate_main, start=pad1, end=pad1)
    async def main_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield parent_method

    @orca.thread(labware=plate_child, start=pad2, end=pad2)
    async def child_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join()

    @orca.workflow(name="auto_spawn_recovery_test")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(main_thread)
        wf.thread(child_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="",
        labwares=[plate_main, plate_child],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    runtime = SystemRuntime(system, event_bus=event_bus)
    return runtime, workflow, dev, event_bus


# ===========================================================================
# 1. Auto-spawn + error recovery
# ===========================================================================

class TestAutoSpawnErrorRecovery:

    @pytest.mark.asyncio
    async def test_auto_spawn_retry_does_not_duplicate(self) -> None:
        """Retry after failure must not create a second auto-spawned child.
        Both owner and contributor pause on shared action failure.
        User recovers all paused threads, then workflow completes."""
        runtime, workflow, device, _ = await _build_auto_spawn_system()
        await runtime.start()

        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        await wait_for_paused_threads(runtime, submission.execution_id, count=2)

        threads_before = runtime.list_threads(submission.execution_id)
        count_before = len(threads_before)

        device.should_fail = False

        for t in runtime.get_paused_threads(submission.execution_id):
            runtime.recover_thread(submission.execution_id, t.id, RecoveryDecision.RETRY)

        await execution_outcome(runtime, submission, timeout=15.0)

        threads_after = runtime.list_threads(submission.execution_id)
        assert len(threads_after) == count_before, (
            f"Thread count changed from {count_before} to {len(threads_after)} after retry"
        )
        await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_auto_spawn_skip_does_not_duplicate(self) -> None:
        """ABORT_ACTION after failure must not create a second child.
        Both threads pause; user recovers all. Second thread's recovery
        is a no-op (stale generation)."""
        runtime, workflow, device, _ = await _build_auto_spawn_system()
        await runtime.start()

        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        await wait_for_paused_threads(runtime, submission.execution_id, count=2)

        threads_before = runtime.list_threads(submission.execution_id)
        count_before = len(threads_before)

        device.should_fail = False

        for t in runtime.get_paused_threads(submission.execution_id):
            runtime.recover_thread(submission.execution_id, t.id, RecoveryDecision.ABORT_ACTION)

        await execution_outcome(runtime, submission, timeout=15.0)

        threads_after = runtime.list_threads(submission.execution_id)
        assert len(threads_after) == count_before, (
            f"Thread count changed from {count_before} to {len(threads_after)} after skip"
        )
        await runtime.shutdown()


# ===========================================================================
# 2. Recursive auto-spawn (depth > 1)
# ===========================================================================

class TestRecursiveAutoSpawn:

    @pytest.mark.asyncio
    async def test_recursive_auto_spawn_depth_2(self) -> None:
        """Thread A spawns B (needs labware_B), B spawns C (needs labware_C)."""
        device = UniversalMockDevice("shaker1", site_names=["site-1", "site-2", "site-3"])
        transporter = create_test_transporter("robot1", ["shaker1", "pad1", "pad2", "pad3"])
        plate_a = create_test_plate_template("plate_a")
        plate_b = create_test_plate_template("plate_b")
        plate_c = create_test_plate_template("plate_c")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("shaker1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(
            system_map, devices={"shaker1": device}, pads=["pad1", "pad2", "pad3"],
        )

        @orca.action(device=pool, inputs=[plate_a, plate_b])
        async def action_ab(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=100)

        @orca.action(device=pool, inputs=[plate_b, plate_c])
        async def action_bc(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=100)

        @orca.method
        async def method_ab(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield action_ab

        @orca.method
        async def method_bc(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield action_bc

        pad1 = system_map.get_location("pad1")
        pad2 = system_map.get_location("pad2")
        pad3 = system_map.get_location("pad3")

        @orca.thread(labware=plate_a, start=pad1, end=pad1)
        async def thread_a(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield method_ab

        @orca.thread(labware=plate_b, start=pad2, end=pad2)
        async def thread_b(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield orca.join()
            yield method_bc

        @orca.thread(labware=plate_c, start=pad3, end=pad3)
        async def thread_c(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield orca.join()

        @orca.workflow(name="recursive_spawn_test")
        def workflow(wf: WorkflowContext) -> None:
            wf.start(thread_a)
            wf.thread(thread_b)
            wf.thread(thread_c)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="recursive_test", description="",
            labwares=[plate_a, plate_b, plate_c],
            resources_registry=registry,
            system_map=system_map,
            workflows=[workflow],
            event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=15.0)

        threads = runtime.list_threads(submission.execution_id)
        assert len(threads) == 3, (
            f"Expected 3 threads (A + auto-spawned B + auto-spawned C), got {len(threads)}"
        )
        assert status.status == "completed"
        await runtime.shutdown()


# ===========================================================================
# 3. Multi-workflow auto-spawn isolation
# ===========================================================================

class TestAutoSpawnRegistryIsolation:

    def test_auto_spawn_registry_is_per_workflow(self) -> None:
        """Each workflow template has its own auto-spawn registry.
        Two workflows using the same labware templates do not share registries."""
        plate_a = create_test_plate_template("plate_a")
        plate_b = create_test_plate_template("plate_b")
        device = UniversalMockDevice("dev")
        pool = ResourcePool("dev", [device])

        @orca.action(device=pool, inputs=[plate_a, plate_b])
        async def action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=100)

        @orca.method
        async def m(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield action

        @orca.thread(labware=plate_a, start="pad1", end="pad1")
        async def thread_a(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield m

        @orca.thread(labware=plate_b, start="pad2", end="pad2")
        async def thread_b(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield orca.join()

        @orca.workflow(name="wf1")
        def wf1(wf: WorkflowContext) -> None:
            wf.start(thread_a)
            wf.thread(thread_b)

        @orca.workflow(name="wf2")
        def wf2(wf: WorkflowContext) -> None:
            wf.start(thread_a)
            wf.thread(thread_b)

        assert wf1.auto_spawn_registry is not wf2.auto_spawn_registry
        assert "plate_b" in wf1.auto_spawn_registry
        assert "plate_b" in wf2.auto_spawn_registry


# ===========================================================================
# 5. ctx.wait_for() in ActionContext
# ===========================================================================

class TestActionContextWaitFor:

    @pytest.mark.asyncio
    async def test_action_context_wait_for_receives_event(self) -> None:
        """ctx.wait_for() in ActionContext returns published event value and data."""
        reg = EventChannelRegistry()
        ctx = ActionContext(
            device_name="test_device",
            action_queue=asyncio.Queue(),
            assigned_labware={},
            variable_store=NullVariableResolver(),
            execution_id="exec-1",
            event_channel_registry=reg,
        )

        channel = reg.get_or_create("signal")
        await channel.publish(value="ready", data={"temp": 22.0})

        value, data = await ctx.wait_for("signal", timeout=1.0)
        assert value == "ready"
        assert data == {"temp": 22.0}

    @pytest.mark.asyncio
    async def test_action_context_wait_for_timeout(self) -> None:
        """ctx.wait_for() raises TimeoutError if event never arrives."""
        reg = EventChannelRegistry()
        ctx = ActionContext(
            device_name="test_device",
            action_queue=asyncio.Queue(),
            assigned_labware={},
            variable_store=NullVariableResolver(),
            execution_id="exec-1",
            event_channel_registry=reg,
        )

        with pytest.raises(asyncio.TimeoutError):
            await ctx.wait_for("never", timeout=0.1)


# ===========================================================================
# 6. Action tag field
# ===========================================================================

class TestActionTag:

    def test_action_template_tag_default_none(self) -> None:
        """Action has tag=None by default."""
        plate = create_test_plate_template("plate")
        device = UniversalMockDevice("dev")
        pool = ResourcePool("dev", [device])

        @orca.action(device=pool, inputs=[plate])
        async def shake(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        assert shake.tag is None

    async def test_setter_tag_propagates_to_runtime_action(self) -> None:
        """A tag set after construction flows onto the runtime action.

        The setter is only useful if downstream factories read it; this
        drives ``MethodActionFactory`` (the production path that builds
        the runtime ``UnresolvedLocationAction``) and asserts the tag
        survives, which is what mutation anchoring keys off.
        """
        plate = create_test_plate_template("plate")
        device = UniversalMockDevice("dev")
        pool = ResourcePool("dev", [device])

        @orca.action(device=pool, inputs=[plate])
        async def shake(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        shake.tag = "my_tag"
        runtime_action = MethodActionFactory(shake).create_instance()
        assert runtime_action.tag == "my_tag"

    def test_orca_action_decorator_with_tag(self) -> None:
        """@orca.action(tag='...') sets the tag on the Action template."""
        plate = create_test_plate_template("plate")
        device = UniversalMockDevice("dev")
        pool = ResourcePool("dev", [device])

        @orca.action(device=pool, inputs=[plate], tag="custom_tag")
        async def tagged_action(ctx: ActionContext) -> None:
            pass

        assert tagged_action.tag == "custom_tag"


# ===========================================================================
# 7. Method auto-registration in TemplateRegistry
# ===========================================================================

class TestMethodAutoRegistration:

    @pytest.mark.asyncio
    async def test_methods_registered_after_execution(self) -> None:
        """Methods are auto-registered in TemplateRegistry during execution."""
        device = UniversalMockDevice("shaker1")
        transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
        plate = create_test_plate_template("plate")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("shaker1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

        @orca.action(device=pool, inputs=[plate])
        async def action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=100)

        @orca.method
        async def my_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield action

        pad1 = system_map.get_location("pad1")

        @orca.thread(labware=plate, start=pad1, end=pad1)
        async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield my_method

        @orca.workflow(name="reg_test")
        def workflow(wf: WorkflowContext) -> None:
            wf.start(plate_thread)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="reg_test", description="",
            labwares=[plate],
            resources_registry=registry,
            system_map=system_map,
            workflows=[workflow],
            event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        # @orca.method decorator registers the template via the pending-list
        # drain in SdkToSystemBuilder.get_system(); no generator introspection.
        # By the time the builder finishes, decorated methods are in the registry.
        templates = system.get_method_templates()
        # Registry keys by (workflow_name, method_name) so two workflows can
        # share a method name.
        assert ("reg_test", "my_method") in templates
        assert len(templates) == 1

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        await execution_outcome(runtime, submission, timeout=10.0)

        # Still registered after execution (idempotent).
        assert ("reg_test", "my_method") in system.get_method_templates()
        await runtime.shutdown()
