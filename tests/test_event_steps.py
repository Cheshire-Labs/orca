"""Tests for Event coordination, yield threads, ctx.emit().

Tests cover:
- ctx.emit() publishing to EventChannelRegistry
- WaitStepTemplate / BranchStepTemplate construction
- orca.on() / orca.branch() SDK surface
- Thread-level event step resolution (wait, branch, timeout, retry)
- @orca.thread yield thread execution
- Cross-thread integration (emit in one thread, wait in another)
"""

import asyncio
from typing import AsyncGenerator, ClassVar, List

import pytest

import orca.orca as orca
from orca.events.event_channel import EventChannel, EventChannelRegistry
from orca.devices.device_interfaces import IShaker
from orca.resource_models.devices import Device
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.variables.variable_store import NullVariableResolver
from orca.workflow_models.device_handle import ActionRequest, DeviceHandle
from orca.workflow_models.event_step import BranchStepTemplate, WaitStepTemplate
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_template import MethodFunc, IMethodTemplate, MethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.status_enums import LabwareThreadStatus, RecoveryDecision, WorkflowStatus
from orca.runtime.run_modes import WorkflowRunMode
from tests.mock import UniversalMockDevice, UniversalSimDriver
from tests.test_helpers import create_test_plate_template, create_test_transporter, execution_outcome, wait_for_runtime_condition, wait_until, wire_system_map


async def _noop_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
    """Noop method generator for tests that need a named method without behavior."""
    return
    yield  # makes this an async generator that yields nothing


def _make_noop_method(name: str) -> MethodTemplate:
    """Create a named noop MethodTemplate for testing."""
    return MethodTemplate(name, func=_noop_method)


def _has_paused_thread(runtime: SystemRuntime, execution_id: str) -> bool:
    """True once any thread in the execution has reached PAUSED."""
    return any(
        t.status == "PAUSED"
        for t in runtime.get_execution_detail(execution_id).threads
    )


async def _await_event_waiters(
    ctx: MethodContext, event_name: str, count: int = 1, timeout: float = 10.0,
) -> None:
    """Block until ``count`` threads have parked on ``event_name``'s channel.

    The channel's parked-waiter count is the deterministic signal that a
    sibling thread has actually blocked on the event; publish is latch-safe,
    so this reproduces the "event arrives after the waiter waits" ordering
    without a wall-clock guess.
    """
    reg = ctx._event_channel_registry
    assert reg is not None
    channel = reg.get_or_create(event_name)
    await wait_until(
        lambda: channel.waiter_count >= count, timeout=timeout
    )


# ---------------------------------------------------------------------------
# ctx.emit()
# ---------------------------------------------------------------------------


class TestCtxEmit:

    def _make_ctx(
        self,
        registry: EventChannelRegistry | None = None,
    ) -> tuple[MethodContext, EventChannelRegistry]:
        queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
        reg = registry or EventChannelRegistry()
        ctx = MethodContext(
            action_queue=queue,
            assigned_labware={},
            variable_store=NullVariableResolver(),
            execution_id="test-exec-1",
            event_channel_registry=reg,
        )
        return ctx, reg

    @pytest.mark.asyncio
    async def test_emit_publishes_to_channel(self) -> None:
        """ctx.emit() should publish to the named channel in the registry."""
        ctx, reg = self._make_ctx()

        received: list[tuple[object, dict[str, object]]] = []

        async def waiter() -> None:
            channel = reg.get_or_create("conc_result")
            _, value, data = await channel.wait(seen_counter=0, timeout=2.0)
            received.append((value, data))

        task = asyncio.create_task(waiter())
        await wait_until(
            lambda: reg.get_or_create("conc_result").waiter_count >= 1,
            timeout=5.0,
        )
        await ctx.emit("conc_result", value="pass", data={"well": "A1"})
        await task

        assert len(received) == 1
        assert received[0] == ("pass", {"well": "A1"})

    @pytest.mark.asyncio
    async def test_emit_creates_channel_if_missing(self) -> None:
        """ctx.emit() should create the channel if it doesn't exist yet."""
        ctx, reg = self._make_ctx()
        await ctx.emit("new_event", value="hello")
        channel = reg.get_or_create("new_event")
        assert channel.counter == 1

    @pytest.mark.asyncio
    async def test_emit_default_values(self) -> None:
        """ctx.emit() with no value/data: value defaults to None, data to {}."""
        ctx, reg = self._make_ctx()
        await ctx.emit("signal")
        channel = reg.get_or_create("signal")
        _, value, data = await channel.wait(seen_counter=0, timeout=1.0)
        assert value is None
        assert data == {}

    @pytest.mark.asyncio
    async def test_emit_multiple_events_different_channels(self) -> None:
        """ctx.emit() to different channel names should publish independently."""
        ctx, reg = self._make_ctx()
        await ctx.emit("event_a", value="a_val")
        await ctx.emit("event_b", value="b_val")

        ch_a = reg.get_or_create("event_a")
        ch_b = reg.get_or_create("event_b")
        assert ch_a.counter == 1
        assert ch_b.counter == 1

    @pytest.mark.asyncio
    async def test_emit_shared_registry_across_contexts(self) -> None:
        """Two MethodContexts sharing the same registry can communicate."""
        reg = EventChannelRegistry()
        ctx1, _ = self._make_ctx(registry=reg)
        ctx2, _ = self._make_ctx(registry=reg)

        received: list[object] = []

        async def wait_on_ctx2_channel() -> None:
            channel = reg.get_or_create("shared_signal")
            _, value, _ = await channel.wait(seen_counter=0, timeout=2.0)
            received.append(value)

        task = asyncio.create_task(wait_on_ctx2_channel())
        await wait_until(
            lambda: reg.get_or_create("shared_signal").waiter_count >= 1,
            timeout=5.0,
        )
        await ctx1.emit("shared_signal", value="from_ctx1")
        await task

        assert received == ["from_ctx1"]


# ---------------------------------------------------------------------------
# WaitStepTemplate + BranchStepTemplate
# ---------------------------------------------------------------------------


class TestWaitStepTemplate:

    def test_properties(self) -> None:
        from orca.workflow_models.event_step import WaitStepTemplate
        step = WaitStepTemplate(event_name="conc_result", timeout=300.0)
        assert step.event_name == "conc_result"
        assert step.timeout == 300.0
        assert step.name == "on:conc_result"

    def test_default_timeout_none(self) -> None:
        from orca.workflow_models.event_step import WaitStepTemplate
        step = WaitStepTemplate(event_name="signal")
        assert step.timeout is None

    def test_is_imethodtemplate(self) -> None:
        from orca.workflow_models.event_step import WaitStepTemplate
        from orca.workflow_models.method_template import IMethodTemplate
        step = WaitStepTemplate(event_name="signal")
        assert isinstance(step, IMethodTemplate)


class TestBranchStepTemplate:

    def test_properties(self) -> None:
        from orca.workflow_models.event_step import BranchStepTemplate
        m1 = _make_noop_method("dilute")
        m2 = _make_noop_method("seal")
        step = BranchStepTemplate(
            event_name="conc_result",
            branches={"dilute": [m1, m2], "proceed": [m2]},
            timeout=60.0,
        )
        assert step.event_name == "conc_result"
        assert step.timeout == 60.0
        assert "dilute" in step.branches
        assert "proceed" in step.branches
        assert step.name == "branch:conc_result"

    def test_default_timeout_none(self) -> None:
        from orca.workflow_models.event_step import BranchStepTemplate
        step = BranchStepTemplate(event_name="signal", branches={})
        assert step.timeout is None

    def test_is_imethodtemplate(self) -> None:
        from orca.workflow_models.event_step import BranchStepTemplate
        from orca.workflow_models.method_template import IMethodTemplate
        step = BranchStepTemplate(event_name="signal", branches={})
        assert isinstance(step, IMethodTemplate)


# ---------------------------------------------------------------------------
# orca.on() / orca.branch() SDK
# ---------------------------------------------------------------------------


class TestOrcaSdkSurface:

    def test_orca_on_returns_wait_step(self) -> None:
        import orca.orca as orca
        from orca.workflow_models.event_step import WaitStepTemplate
        step = orca.on("conc_result", timeout=300.0)
        assert isinstance(step, WaitStepTemplate)
        assert step.event_name == "conc_result"
        assert step.timeout == 300.0

    def test_orca_on_default_timeout(self) -> None:
        import orca.orca as orca
        step = orca.on("signal")
        assert step.timeout is None

    def test_orca_branch_returns_branch_step(self) -> None:
        import orca.orca as orca
        from orca.workflow_models.event_step import BranchStepTemplate
        m1 = _make_noop_method("dilute")
        step = orca.branch("conc_result", branches={"dilute": [m1]}, timeout=60.0)
        assert isinstance(step, BranchStepTemplate)
        assert step.event_name == "conc_result"
        assert step.timeout == 60.0

    def test_orca_branch_default_timeout(self) -> None:
        import orca.orca as orca
        step = orca.branch("signal", branches={})
        assert step.timeout is None


# ---------------------------------------------------------------------------
# Factory chain: ThreadFactory creates event step instances
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Integration: WaitStep blocks thread until event published
# ---------------------------------------------------------------------------


async def _build_event_step_system(
    thread_steps: list[IMethodTemplate],
    emitter_func: MethodFunc | None = None,
) -> tuple[SystemRuntime, WorkflowTemplate]:
    """Build a system with one thread that has event steps.

    If emitter_func is provided, a second thread with a code method is created
    that can emit events to unblock the first thread.
    """
    device = UniversalMockDevice("device1")
    transporter = create_test_transporter("robot1", ["device1", "pad1", "pad2"])
    plate1 = create_test_plate_template("plate_a")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("device1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map, devices={"device1": device}, pads=["pad1", "pad2"],
    )

    pad1 = system_map.get_location("pad1")

    captured_steps = list(thread_steps)

    async def _thread_a_gen(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        for step in captured_steps:
            yield step

    thread_a = ThreadTemplate(
        labware_template=plate1,
        start=pad1,
        end=pad1,
        func=_thread_a_gen,
    )

    workflow = WorkflowTemplate("event_step_test")
    workflow.add_thread(thread_a, is_start=True)

    all_methods = [m for m in thread_steps if isinstance(m, MethodTemplate)]
    all_labwares = [plate1]

    if emitter_func is not None:
        plate2 = create_test_plate_template("plate_b")
        pad2 = system_map.get_location("pad2")

        @orca.action(device=pool, inputs=[plate2])
        async def noop_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=0, speed=0)

        original_emitter = emitter_func
        async def _emitter_method_gen(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield noop_action
            await original_emitter(ctx)

        emitter_method = orca.method()(_emitter_method_gen)

        async def _emitter_gen(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield emitter_method

        thread_b = ThreadTemplate(
            labware_template=plate2,
            start=pad2,
            end=pad2,
            func=_emitter_gen,
        )
        workflow.add_thread(thread_b, is_start=True)
        all_methods.append(emitter_method)
        all_labwares.append(plate2)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="",
        labwares=all_labwares,
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    runtime = SystemRuntime(system, event_bus=event_bus)
    return runtime, workflow


# ---------------------------------------------------------------------------
# ThreadContext (for @orca.thread yield threads)
# ---------------------------------------------------------------------------


class TestThreadContext:

    @pytest.mark.asyncio
    async def test_emit_publishes_to_channel(self) -> None:

        reg = EventChannelRegistry()
        ctx = ThreadContext(reg, NullVariableResolver(), "exec-1")

        await ctx.emit("signal", value="go")
        channel = reg.get_or_create("signal")
        assert channel.counter == 1

    @pytest.mark.asyncio
    async def test_on_returns_value_and_data(self) -> None:

        reg = EventChannelRegistry()
        ctx = ThreadContext(reg, NullVariableResolver(), "exec-1")

        channel = reg.get_or_create("result")
        await channel.publish(value="pass", data={"well": "A1"})

        value, data = await ctx.wait_for("result", timeout=1.0)
        assert value == "pass"
        assert data == {"well": "A1"}

    @pytest.mark.asyncio
    async def test_on_consumed_semantics(self) -> None:
        """Each ctx.wait_for() call should see the NEXT publish, not the same one."""

        reg = EventChannelRegistry()
        ctx = ThreadContext(reg, NullVariableResolver(), "exec-1")
        channel = reg.get_or_create("result")

        await channel.publish(value="first")
        value1, _ = await ctx.wait_for("result", timeout=1.0)
        assert value1 == "first"

        # Second on() should block until a new publish
        await channel.publish(value="second")
        value2, _ = await ctx.wait_for("result", timeout=1.0)
        assert value2 == "second"

    @pytest.mark.asyncio
    async def test_on_timeout_raises(self) -> None:

        reg = EventChannelRegistry()
        ctx = ThreadContext(reg, NullVariableResolver(), "exec-1")

        with pytest.raises(asyncio.TimeoutError):
            await ctx.wait_for("never", timeout=0.1)

    async def test_param_delegates_to_variable_store(self) -> None:

        from orca.variables.errors import UndefinedVariableError
        reg = EventChannelRegistry()
        ctx = ThreadContext(reg, NullVariableResolver(), "exec-1")

        with pytest.raises(UndefinedVariableError):
            await ctx.param("undefined")


# ---------------------------------------------------------------------------
# Integration: WaitStep blocks thread until event published
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# @orca.thread yield thread integration
# ---------------------------------------------------------------------------


class TestYieldThreadIntegration:

    @pytest.mark.asyncio
    async def test_yield_thread_executes_yielded_methods(self) -> None:
        """A yield thread should execute methods yielded by the generator."""
        shake = _make_noop_method("shake")

        async def my_thread(ctx: "ThreadContext") -> "AsyncGenerator[MethodTemplate, None]":
            yield shake


        from typing import AsyncGenerator

        device = UniversalMockDevice("device1")
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_a")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)

        system_map = SystemMap(registry)
        await wire_system_map(
            system_map, devices={"device1": device}, pads=["pad1"],
        )
        pad1 = system_map.get_location("pad1")

        yield_template = ThreadTemplate(
            labware_template=plate, start=pad1, end=pad1, func=my_thread,
        )

        workflow = WorkflowTemplate("yield_test")
        workflow.add_thread(yield_template, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="",
            labwares=[plate], resources_registry=registry,
            system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)
        assert status.status == "completed", f"Expected completed, got {status.status}: {status.error}"
        await runtime.shutdown()


# ---------------------------------------------------------------------------
# Integration: WaitStep blocks thread until event published
# ---------------------------------------------------------------------------


class TestWaitStepIntegration:

    @pytest.mark.asyncio
    async def test_wait_step_blocks_until_event_published(self) -> None:
        """Thread with WaitStep should block until event is published."""
        shake = _make_noop_method("shake")

        async def emitter(ctx: MethodContext) -> None:
            await _await_event_waiters(ctx, "signal")
            await ctx.emit("signal", value="go")

        runtime, workflow = await _build_event_step_system(
            thread_steps=[shake, orca.on("signal")],
            emitter_func=emitter,
        )

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)
        assert status.status == "completed", f"Expected completed, got {status.status}: {status.error}"
        await runtime.shutdown()

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    @pytest.mark.slow
    async def test_wait_step_timeout_pauses_thread(self) -> None:
        """WaitStep with short timeout should pause the thread."""
        shake = _make_noop_method("shake")
        runtime, workflow = await _build_event_step_system(
            thread_steps=[shake, orca.on("never_arrives", timeout=0.5)],
        )

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        # Wait for timeout to fire and thread to pause
        await wait_for_runtime_condition(
            runtime,
            lambda: _has_paused_thread(runtime, submission.execution_id),
            timeout=10.0,
        )

        detail = runtime.get_execution_detail(submission.execution_id)
        paused_threads = [t for t in detail.threads if t.status == "PAUSED"]
        assert len(paused_threads) >= 1
        await runtime.shutdown()


class TestBranchStepIntegration:

    @pytest.mark.asyncio
    async def test_branch_selects_correct_branch(self) -> None:
        """BranchStep selects the branch matching the event value; the other does not run."""
        executed: list[str] = []

        @orca.method()
        async def do_seal(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            executed.append("seal")
            return
            yield

        @orca.method()
        async def do_shake(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            executed.append("shake")
            return
            yield

        async def emitter(ctx: MethodContext) -> None:
            await _await_event_waiters(ctx, "result")
            await ctx.emit("result", value="seal_it")

        runtime, workflow, device = await _build_tracking_system(
            thread_steps=[
                orca.branch("result", {"seal_it": [do_seal], "shake_it": [do_shake]})
            ],
            emitter_func=emitter,
        )

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)
        assert status.status == "completed", f"Expected completed, got {status.status}: {status.error}"
        assert "seal" in executed, "Matched seal_it branch did not run"
        assert "shake" not in executed, "Unmatched shake_it branch ran"
        await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_branch_else_fallback(self) -> None:
        """An unmatched value runs the 'else' branch, not the named branch."""
        executed: list[str] = []

        @orca.method()
        async def do_known(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            executed.append("known")
            return
            yield

        @orca.method()
        async def do_else(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            executed.append("else")
            return
            yield

        async def emitter(ctx: MethodContext) -> None:
            await _await_event_waiters(ctx, "result")
            await ctx.emit("result", value="unexpected_value")

        runtime, workflow, device = await _build_tracking_system(
            thread_steps=[
                orca.branch("result", {"known": [do_known], "else": [do_else]})
            ],
            emitter_func=emitter,
        )

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)
        assert status.status == "completed", f"Expected completed, got {status.status}: {status.error}"
        assert "else" in executed, "Else fallback branch did not run for unmatched value"
        assert "known" not in executed, "Named 'known' branch ran for an unmatched value"
        await runtime.shutdown()


# ---------------------------------------------------------------------------
# Reviewer tests: behavioral verification + error paths
# ---------------------------------------------------------------------------


class TrackingMockDevice(Device, IShaker):
    """Mock device that records every command invocation for verification."""

    KIND: ClassVar[str] = "mock"

    def __init__(self, name: str) -> None:
        driver = UniversalSimDriver(name)
        super().__init__(name, driver, driver)
        self.calls: list[str] = []

    async def shake(self, duration: int, speed: int) -> None:
        from cheshire_drivers.shaker_models import ShakeRequest
        self.calls.append(f"shake({duration},{speed})")
        await self.driver.shake(ShakeRequest(speed=float(speed), duration=float(duration)))

    async def seal(self, temperature: int, duration: float) -> None:
        from cheshire_drivers.sealer_models import SealRequest
        self.calls.append(f"seal({temperature},{duration})")
        await self.driver.seal(SealRequest(temperature=temperature, duration=duration))


async def _build_tracking_system(
    thread_steps: list[IMethodTemplate],
    emitter_func: MethodFunc | None = None,
    second_thread_steps: list[IMethodTemplate] | None = None,
) -> tuple[SystemRuntime, WorkflowTemplate, TrackingMockDevice]:
    """Build system with a TrackingMockDevice for behavioral verification."""
    device = TrackingMockDevice("device1")
    position_ids = ["device1", "pad1", "pad2"]
    if second_thread_steps is not None:
        position_ids.append("pad3")
    transporter = create_test_transporter("robot1", position_ids)
    plate1 = create_test_plate_template("plate_a")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("device1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map,
        devices={"device1": device},
        pads=[p for p in position_ids if p != "device1"],
    )

    pad1 = system_map.get_location("pad1")

    captured_steps_a = list(thread_steps)

    async def _thread_a_gen(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        for step in captured_steps_a:
            yield step

    thread_a = ThreadTemplate(
        labware_template=plate1, start=pad1, end=pad1,
        func=_thread_a_gen,
    )

    workflow = WorkflowTemplate("tracking_test")
    workflow.add_thread(thread_a, is_start=True)

    all_methods = [m for m in thread_steps if isinstance(m, MethodTemplate)]
    all_labwares = [plate1]

    if emitter_func is not None:
        plate2 = create_test_plate_template("plate_b")
        pad2 = system_map.get_location("pad2")

        @orca.action(device=pool, inputs=[plate2])
        async def noop_action_2(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=0, speed=0)

        original_emitter_2 = emitter_func
        async def _emitter_method_gen_2(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield noop_action_2
            await original_emitter_2(ctx)

        emitter_method = orca.method()(_emitter_method_gen_2)

        async def _emitter_gen(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield emitter_method

        thread_b = ThreadTemplate(
            labware_template=plate2, start=pad2, end=pad2,
            func=_emitter_gen,
        )
        workflow.add_thread(thread_b, is_start=True)
        all_methods.append(emitter_method)
        all_labwares.append(plate2)

    if second_thread_steps is not None:
        plate3 = create_test_plate_template("plate_c")
        pad3 = system_map.get_location("pad3")
        captured_steps_c = list(second_thread_steps)

        async def _thread_c_gen(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            for step in captured_steps_c:
                yield step

        thread_c = ThreadTemplate(
            labware_template=plate3, start=pad3, end=pad3,
            func=_thread_c_gen,
        )
        workflow.add_thread(thread_c, is_start=True)
        second_methods = [m for m in second_thread_steps if isinstance(m, MethodTemplate)]
        all_methods.extend(second_methods)
        all_labwares.append(plate3)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="",
        labwares=all_labwares, resources_registry=registry,
        system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    runtime = SystemRuntime(system, event_bus=event_bus)
    return runtime, workflow, device


class TestReviewerBehavioralVerification:

    @pytest.mark.asyncio
    async def test_yield_thread_method_actually_executes(self) -> None:
        """Verify yield thread method runs via side effect tracking."""
        executed: list[str] = []

        device = UniversalMockDevice("device1")
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_a")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)

        system_map = SystemMap(registry)
        await wire_system_map(
            system_map, devices={"device1": device}, pads=["pad1"],
        )
        pad1 = system_map.get_location("pad1")

        @orca.action(device=pool, inputs=[plate])
        async def shake_action(ctx: object) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.method()
        async def tracked_shake(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            executed.append("shake_ran")
            yield shake_action

        async def my_thread(ctx: ThreadContext) -> "AsyncGenerator":
            yield tracked_shake

        yield_template = ThreadTemplate(
            labware_template=plate, start=pad1, end=pad1, func=my_thread,
        )

        workflow = WorkflowTemplate("yield_proof_test")
        workflow.add_thread(yield_template, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="",
            labwares=[plate], resources_registry=registry,
            system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)
        assert status.status == "completed"
        assert "shake_ran" in executed, "Code method inside yield thread didn't execute"
        await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_branch_runs_correct_branch_not_other(self) -> None:
        """Verify branch takes the correct path by checking side effects."""
        executed: list[str] = []

        @orca.method()
        async def do_seal(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            executed.append("seal")
            return
            yield

        @orca.method()
        async def do_shake(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            executed.append("shake")
            return
            yield

        async def emitter(ctx: MethodContext) -> None:
            await _await_event_waiters(ctx, "result")
            await ctx.emit("result", value="seal_it")

        runtime, workflow, device = await _build_tracking_system(
            thread_steps=[
                orca.branch("result", {"seal_it": [do_seal], "shake_it": [do_shake]})
            ],
            emitter_func=emitter,
        )

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)
        assert status.status == "completed"
        assert "seal" in executed, "Seal branch was not executed"
        assert "shake" not in executed, "Shake branch was incorrectly executed"
        await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_wait_step_actually_blocks_then_continues(self) -> None:
        """Verify the wait step BLOCKS: after_wait must not run before emit.

        A no-op `on()` would let after_wait run immediately at ~t=0, before
        the emitter publishes the signal at ~t=0.5. Recording the ordered
        sequence of emit vs after_wait makes the causality observable: a
        broken (non-blocking) wait flips the order and fails this test."""
        sequence: list[str] = []

        @orca.method()
        async def after_wait(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            sequence.append("after_wait")
            return
            yield

        async def emitter(ctx: MethodContext) -> None:
            await _await_event_waiters(ctx, "signal")
            sequence.append("emitted")
            await ctx.emit("signal", value="go")

        runtime, workflow, device = await _build_tracking_system(
            thread_steps=[orca.on("signal"), after_wait],
            emitter_func=emitter,
        )

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)
        assert status.status == "completed"
        assert "after_wait" in sequence, "Method after wait didn't execute"
        assert sequence.index("emitted") < sequence.index("after_wait"), (
            f"after_wait ran before the signal was emitted; wait did not block: {sequence}"
        )
        await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_two_threads_both_receive_same_event(self) -> None:
        """Two threads waiting on the same event should both unblock."""
        executed: list[str] = []

        @orca.method()
        async def thread_a_work(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            executed.append("thread_a")
            return
            yield

        @orca.method()
        async def thread_c_work(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            executed.append("thread_c")
            return
            yield

        async def emitter(ctx: MethodContext) -> None:
            await _await_event_waiters(ctx, "signal", count=2)
            await ctx.emit("signal", value="go")

        runtime, workflow, device = await _build_tracking_system(
            thread_steps=[orca.on("signal"), thread_a_work],
            emitter_func=emitter,
            second_thread_steps=[orca.on("signal"), thread_c_work],
        )

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)
        assert status.status == "completed"
        assert "thread_a" in executed, "Thread A didn't execute after event"
        assert "thread_c" in executed, "Thread C didn't execute after event"
        await runtime.shutdown()


class TestReviewerErrorPaths:

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    @pytest.mark.slow
    async def test_yield_func_exception_pauses_thread(self) -> None:
        """If yield function raises, thread should pause for error recovery."""


        async def crashing_thread(ctx: ThreadContext) -> "AsyncGenerator":
            raise RuntimeError("generator bug")
            yield  # pragma: no cover

        device = UniversalMockDevice("device1")
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_a")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)

        system_map = SystemMap(registry)
        await wire_system_map(
            system_map, devices={"device1": device}, pads=["pad1"],
        )
        pad1 = system_map.get_location("pad1")

        yield_template = ThreadTemplate(
            labware_template=plate, start=pad1, end=pad1, func=crashing_thread,
        )

        workflow = WorkflowTemplate("crash_test")
        workflow.add_thread(yield_template, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="",
            labwares=[plate], resources_registry=registry,
            system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        await wait_for_runtime_condition(
            runtime,
            lambda: _has_paused_thread(runtime, submission.execution_id),
            timeout=10.0,
        )

        detail = runtime.get_execution_detail(submission.execution_id)
        paused = [t for t in detail.threads if t.status == "PAUSED"]
        assert len(paused) >= 1, f"Expected PAUSED thread, got statuses: {[t.status for t in detail.threads]}"
        await runtime.shutdown()

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    @pytest.mark.slow
    async def test_branch_no_match_no_else_pauses_thread(self) -> None:
        """Branch with no matching value and no 'else' should pause thread."""
        shake = _make_noop_method("shake")

        async def emitter(ctx: MethodContext) -> None:
            await _await_event_waiters(ctx, "result")
            await ctx.emit("result", value="unknown_value")

        runtime, workflow, device = await _build_tracking_system(
            thread_steps=[
                orca.branch("result", {"a": [shake], "b": [shake]})
            ],
            emitter_func=emitter,
        )

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        await wait_for_runtime_condition(
            runtime,
            lambda: _has_paused_thread(runtime, submission.execution_id),
            timeout=10.0,
        )

        detail = runtime.get_execution_detail(submission.execution_id)
        paused = [t for t in detail.threads if t.status == "PAUSED"]
        assert len(paused) >= 1, f"Expected PAUSED thread, got statuses: {[t.status for t in detail.threads]}"
        await runtime.shutdown()

    @pytest.mark.asyncio
    @pytest.mark.timeout(15)
    @pytest.mark.slow
    async def test_wait_timeout_then_retry_succeeds(self) -> None:
        """Wait timeout -> pause -> operator emits event -> retry -> completes."""
        executed: list[str] = []

        @orca.method()
        async def after_retry(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            executed.append("after_retry")
            return
            yield

        runtime, workflow, device = await _build_tracking_system(
            thread_steps=[orca.on("signal", timeout=0.5), after_retry],
        )

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        await wait_for_runtime_condition(
            runtime,
            lambda: _has_paused_thread(runtime, submission.execution_id),
            timeout=10.0,
        )

        detail = runtime.get_execution_detail(submission.execution_id)
        paused = [t for t in detail.threads if t.status == "PAUSED"]
        assert len(paused) >= 1, "Thread should be paused after timeout"

        # Emit the event manually, then resume with RETRY
        exec_entry = runtime._executions[submission.execution_id]
        assert exec_entry.executing_workflow is not None
        ecr = exec_entry.executing_workflow._event_channel_registry
        channel = ecr.get_or_create("signal")
        await channel.publish(value="go")

        thread_id = paused[0].id
        thread = runtime._find_thread(submission.execution_id, thread_id)
        thread.resume_with_decision(RecoveryDecision.RETRY)

        status = await execution_outcome(runtime, submission, timeout=10.0)
        assert status.status == "completed", f"Expected completed after retry, got {status.status}: {status.error}"
        assert "after_retry" in executed, "Method after retry didn't execute"
        await runtime.shutdown()


# ---------------------------------------------------------------------------
# Skip event steps via skip-set
# ---------------------------------------------------------------------------


class TestSkipEventSteps:

    @pytest.mark.asyncio
    async def test_skip_wait_step_bypasses_event_wait(self) -> None:
        """Skipping a WaitStep by name should bypass the event wait entirely."""
        executed: list[str] = []
        skip_injected = asyncio.Event()
        in_slow_method = asyncio.Event()

        @orca.method()
        async def slow_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            # Announce entry (method_lane now live), then hold until the skip is
            # injected, so it lands before the thread advances to the wait step.
            in_slow_method.set()
            await skip_injected.wait()
            executed.append("slow")
            return
            yield

        @orca.method()
        async def after_skip(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            executed.append("after_skip")
            return
            yield

        runtime, workflow, device = await _build_tracking_system(
            thread_steps=[slow_method, orca.on("never_arrives"), after_skip],
        )

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)

        # Wait until the thread is parked IN slow_method (not merely present), so
        # its method_lane is live before we add the skip.
        await asyncio.wait_for(in_slow_method.wait(), timeout=10.0)
        exec_entry = runtime._executions[submission.execution_id]
        assert exec_entry.executing_workflow is not None
        threads = exec_entry.executing_workflow.threads
        for t in threads:
            if t.name.startswith("plate_a"):
                t.method_lane.add_skip("on:never_arrives")
        skip_injected.set()

        status = await execution_outcome(runtime, submission, timeout=10.0)
        assert status.status == "completed", f"Expected completed, got {status.status}: {status.error}"
        assert "after_skip" in executed, "Method after skipped WaitStep didn't execute"
        await runtime.shutdown()


# ---------------------------------------------------------------------------
# Yield thread ctx.labware() access
# ---------------------------------------------------------------------------


class TestYieldThreadLabware:

    @pytest.mark.asyncio
    async def test_yield_thread_code_method_can_access_labware(self) -> None:
        """Method yielded by yield thread should execute actions on the device."""
        device = TrackingMockDevice("device1")
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_a")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)

        system_map = SystemMap(registry)
        await wire_system_map(
            system_map, devices={"device1": device}, pads=["pad1"],
        )
        pad1 = system_map.get_location("pad1")

        @orca.action(device=pool, inputs=[plate])
        async def check_labware_action(ctx: object) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.method
        async def check_labware(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield check_labware_action

        @orca.thread(labware=plate, start=pad1, end=pad1)
        async def my_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield check_labware

        workflow = WorkflowTemplate("labware_test")
        workflow.add_thread(my_thread, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="",
            labwares=[plate], resources_registry=registry,
            system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)
        assert status.status == "completed", f"Expected completed, got {status.status}: {status.error}"
        assert device.calls == ["shake(1,500)"], (
            f"yielded method did not run its device action; recorded: {device.calls}"
        )
        await runtime.shutdown()
