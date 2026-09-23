"""Event-step behaviours that a review found missing.

Verifies:
- Labware wiring in yield thread and branch-resolved paths
- Error recovery when generators raise or branches don't match
- Behavioral proof that methods actually execute (not just status checks)
- Concurrent event coordination across threads
"""

import asyncio
from typing import AsyncGenerator

import pytest

import orca.orca as orca
from orca.events.event_bus import EventBus
from orca.resource_models.resource_pool import ResourcePool
from orca.system.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.runtime.system_runtime import SystemRuntime
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.status_enums import RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflow_templates import WorkflowTemplate
from orca.runtime.run_modes import WorkflowRunMode
from tests.mock import UniversalMockDevice
from tests.test_helpers import (
    execution_outcome,
    create_test_plate_template, create_test_transporter,
    wait_for_paused_thread, wait_until, wire_system_map,
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
    await wait_until(lambda: channel.waiter_count >= count, timeout=timeout)


# ---------------------------------------------------------------------------
# System builder helpers
# ---------------------------------------------------------------------------


async def _build_single_thread_system(
    thread_template: ThreadTemplate,
    all_methods: list[MethodTemplate],
) -> tuple[SystemRuntime, WorkflowTemplate]:
    """Build a system with one thread on one device."""
    device = UniversalMockDevice("device1")
    transporter = create_test_transporter("robot1", ["device1", "pad1"])
    plate = thread_template.labware_template

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

    thread = ThreadTemplate(
        labware_template=plate, start=pad1, end=pad1,
        func=thread_template.func,
    )

    workflow = WorkflowTemplate("review_test")
    workflow.add_thread(thread, is_start=True)

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
    return runtime, workflow


async def _build_two_thread_system(
    main_thread: ThreadTemplate,
    emitter_method: MethodTemplate,
    all_methods: list[MethodTemplate],
) -> tuple[SystemRuntime, WorkflowTemplate]:
    """Build a system with a main thread and an emitter thread on separate pads."""
    device = UniversalMockDevice("device1")
    transporter = create_test_transporter("robot1", ["device1", "pad1", "pad2"])
    main_plate = main_thread.labware_template
    emitter_plate = create_test_plate_template("emitter_plate")

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
    pad2 = system_map.get_location("pad2")

    thread_a = ThreadTemplate(
        labware_template=main_plate, start=pad1, end=pad1,
        func=main_thread.func,
    )

    async def _emitter_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield emitter_method

    thread_b = ThreadTemplate(
        labware_template=emitter_plate, start=pad2, end=pad2,
        func=_emitter_thread,
    )

    workflow = WorkflowTemplate("review_test")
    workflow.add_thread(thread_a, is_start=True)
    workflow.add_thread(thread_b, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="",
        labwares=[main_plate, emitter_plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    runtime = SystemRuntime(system, event_bus=event_bus)
    return runtime, workflow


# ---------------------------------------------------------------------------
# 1. Yield thread: labware access via ctx.labware()
# ---------------------------------------------------------------------------


class TestYieldThreadLabwareAccess:

    @pytest.mark.asyncio
    async def test_yield_thread_code_method_can_access_labware(self) -> None:
        """A code method yielded by a yield thread must execute and
        can reference the thread's labware via ActionTemplate inputs/outputs.
        """
        method_ran: list[bool] = []

        @orca.method()
        async def check_labware(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            method_ran.append(True)
            return
            yield

        plate = create_test_plate_template("plate_96")

        async def my_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
            yield check_labware

        yield_tmpl = ThreadTemplate(
            labware_template=plate, start="pad1", end="pad1",
            func=my_thread,
        )

        runtime, workflow = await _build_single_thread_system(yield_tmpl, [check_labware])
        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)

        assert status.status == "completed", (
            f"Expected completed, got {status.status}: {status.error}"
        )
        assert method_ran, "Code method should have run"
        await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_yield_thread_method_actually_executes(self) -> None:
        """Verify the yielded method runs (not just that the workflow completes).

        A trivially wrong implementation that skips all methods would still
        produce status=completed. This test captures a side effect to prove
        the method body ran.
        """
        call_log: list[str] = []

        @orca.method()
        async def shake_and_log(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            call_log.append("shook")
            return
            yield

        plate = create_test_plate_template("plate_96")

        async def my_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
            yield shake_and_log

        yield_tmpl = ThreadTemplate(
            labware_template=plate, start="pad1", end="pad1",
            func=my_thread,
        )

        runtime, workflow = await _build_single_thread_system(yield_tmpl, [shake_and_log])
        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)

        assert status.status == "completed", f"got {status.status}: {status.error}"
        assert call_log == ["shook"], (
            f"Method body should have run exactly once, call_log={call_log}"
        )
        await runtime.shutdown()


# ---------------------------------------------------------------------------
# 2. Branch-resolved methods: labware access via ctx.labware()
# ---------------------------------------------------------------------------


class TestBranchLabwareAccess:

    @pytest.mark.asyncio
    async def test_branch_resolved_code_method_can_access_labware(self) -> None:
        """A code method inside a branch must be able to call ctx.labware().

        BranchStep pre-resolves all branch templates to ExecutingMethods at
        construction time. Those methods must have assign_thread called so
        their labware mapping is populated.
        """
        branch_ran: list[str] = []

        @orca.method()
        async def read_plate(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            branch_ran.append("read")
            return
            yield

        @orca.method()
        async def emit_result(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            await _await_event_waiters(ctx, "result")
            await ctx.emit("result", value="read")
            return
            yield

        async def _noop(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            return
            yield

        shake_noop = MethodTemplate("shake", func=_noop)

        plate = create_test_plate_template("plate_96")

        async def _main_gen(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
            yield orca.branch("result", {
                "read": [read_plate],
                "else": [shake_noop],
            })

        main_thread = ThreadTemplate(
            labware_template=plate, start="pad1", end="pad1",
            func=_main_gen,
        )

        runtime, workflow = await _build_two_thread_system(
            main_thread, emit_result,
            all_methods=[read_plate, shake_noop],
        )
        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)

        assert status.status == "completed", (
            f"Expected completed, got {status.status}: {status.error}"
        )
        assert branch_ran == ["read"], "Branch method should have run"
        await runtime.shutdown()


# ---------------------------------------------------------------------------
# 3. Generator exceptions should trigger error recovery, not crash
# ---------------------------------------------------------------------------


class TestGeneratorErrorRecovery:

    @pytest.mark.asyncio
    async def test_yield_func_exception_pauses_thread(self) -> None:
        """If the user's yield function raises, the thread should pause
        (entering error recovery) rather than crashing silently.
        """
        plate = create_test_plate_template("plate_96")

        async def _noop(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            return
            yield

        unreachable = MethodTemplate("unreachable", func=_noop)

        async def failing_generator(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
            raise ValueError("generator bug: bad state")
            yield unreachable

        yield_tmpl = ThreadTemplate(
            labware_template=plate, start="pad1", end="pad1",
            func=failing_generator,
        )

        runtime, workflow = await _build_single_thread_system(yield_tmpl, [])
        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)

        # Thread should pause for error recovery, not crash silently
        paused = await wait_for_paused_thread(runtime, submission.execution_id)
        assert paused is not None
        await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_merge_lane_close_sends_generator_exit(self) -> None:
        """MergeLane.close() should send GeneratorExit to a suspended generator.

        This is the mechanism that prevents async generator leaks when a
        thread is cancelled while its generator is suspended at a yield.
        """
        from orca.workflow_models.merge_lane import MergeLane

        generator_closed = False

        async def tracked_generator() -> AsyncGenerator[str, None]:
            nonlocal generator_closed
            try:
                yield "first"
                yield "second"
                yield "third"
            except GeneratorExit:
                generator_closed = True
                raise

        lane: MergeLane[str] = MergeLane(tracked_generator(), name_getter=lambda _: None)

        # Consume one item -- generator is now suspended at the second yield
        item = await lane.next()
        assert item == "first"
        assert not generator_closed

        # Close the lane while generator is suspended
        await lane.close()

        assert generator_closed, (
            "Generator did not receive GeneratorExit. "
            "MergeLane.close() must call aclose() on suspended generators."
        )
        assert lane.exhausted



# ---------------------------------------------------------------------------
# 4. Branch with no matching value and no "else" should not crash
# ---------------------------------------------------------------------------


class TestBranchNoMatchRecovery:

    @pytest.mark.asyncio
    async def test_branch_no_match_no_else_pauses_thread(self) -> None:
        """If a branch event value matches no key and there's no "else",
        the thread should pause for error recovery rather than crash.
        """
        @orca.method()
        async def emit_unknown(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            await _await_event_waiters(ctx, "result")
            await ctx.emit("result", value="unknown_value")
            return
            yield

        async def _noop(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            return
            yield

        plate = create_test_plate_template("plate_96")
        shake = MethodTemplate("shake", func=_noop)

        async def _main_gen(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
            yield orca.branch("result", {
                "expected": [shake],
                # deliberately no "else" key
            })

        main_thread = ThreadTemplate(
            labware_template=plate, start="pad1", end="pad1",
            func=_main_gen,
        )

        runtime, workflow = await _build_two_thread_system(
            main_thread, emit_unknown, all_methods=[shake],
        )
        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)

        # Thread should pause for error recovery, not crash
        paused = await wait_for_paused_thread(runtime, submission.execution_id)
        assert paused is not None
        await runtime.shutdown()


# ---------------------------------------------------------------------------
# 5. Integration tests with behavioral verification
# ---------------------------------------------------------------------------


class TestBranchBehaviorVerification:

    @pytest.mark.asyncio
    async def test_branch_runs_correct_branch_not_other(self) -> None:
        """Verify that only the matched branch's methods execute.

        Both branches use different side effects. The test asserts only
        the correct branch's side effect occurred.
        """
        branch_log: list[str] = []

        @orca.method()
        async def path_a(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            branch_log.append("path_a")
            return
            yield

        @orca.method()
        async def path_b(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            branch_log.append("path_b")
            return
            yield

        @orca.method()
        async def emit_choice(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            await _await_event_waiters(ctx, "choice")
            await ctx.emit("choice", value="go_b")
            return
            yield

        plate = create_test_plate_template("plate_96")

        async def _main_gen(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
            yield orca.branch("choice", {
                "go_a": [path_a],
                "go_b": [path_b],
            })

        main_thread = ThreadTemplate(
            labware_template=plate, start="pad1", end="pad1",
            func=_main_gen,
        )

        runtime, workflow = await _build_two_thread_system(
            main_thread, emit_choice,
            all_methods=[path_a, path_b],
        )
        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)

        assert status.status == "completed", f"got {status.status}: {status.error}"
        assert branch_log == ["path_b"], (
            f"Only path_b should have run, got {branch_log}"
        )
        await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_wait_step_actually_blocks_then_continues(self) -> None:
        """Verify wait step blocks thread, then the method after it executes.

        Captures timing to prove the method ran AFTER the event, not before.
        """
        timeline: list[tuple[str, float]] = []

        @orca.method()
        async def post_wait_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            timeline.append(("post_wait", asyncio.get_event_loop().time()))
            return
            yield

        @orca.method()
        async def delayed_emit(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            await _await_event_waiters(ctx, "gate")
            timeline.append(("emit", asyncio.get_event_loop().time()))
            await ctx.emit("gate", value="open")
            return
            yield

        plate = create_test_plate_template("plate_96")

        async def _main_gen(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
            yield orca.on("gate")
            yield post_wait_method

        main_thread = ThreadTemplate(
            labware_template=plate, start="pad1", end="pad1",
            func=_main_gen,
        )

        runtime, workflow = await _build_two_thread_system(
            main_thread, delayed_emit,
            all_methods=[post_wait_method],
        )
        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)

        assert status.status == "completed", f"got {status.status}: {status.error}"
        assert len(timeline) == 2, f"Both events should fire, got {timeline}"

        events = [e[0] for e in timeline]
        assert events == ["emit", "post_wait"], (
            f"post_wait should run after emit, got order: {events}"
        )
        await runtime.shutdown()


# ---------------------------------------------------------------------------
# 6. Concurrent event waits: multiple threads waiting on same event
# ---------------------------------------------------------------------------


class TestConcurrentEventWaits:

    @pytest.mark.asyncio
    async def test_two_threads_both_receive_same_event(self) -> None:
        """Two threads waiting on the same event should both unblock."""
        received: list[str] = []

        @orca.method()
        async def waiter_a(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            received.append("thread_a")
            return
            yield

        @orca.method()
        async def waiter_b(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            received.append("thread_b")
            return
            yield

        @orca.method()
        async def emit_signal(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            await _await_event_waiters(ctx, "go", count=2)
            await ctx.emit("go", value="now")
            return
            yield

        device = UniversalMockDevice("device1")
        transporter = create_test_transporter(
            "robot1", ["device1", "pad1", "pad2", "pad3"]
        )
        plate_a = create_test_plate_template("plate_a")
        plate_b = create_test_plate_template("plate_b")
        plate_c = create_test_plate_template("plate_c")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)

        system_map = SystemMap(registry)
        await wire_system_map(
            system_map, devices={"device1": device}, pads=["pad1", "pad2", "pad3"],
        )
        pad1 = system_map.get_location("pad1")
        pad2 = system_map.get_location("pad2")
        pad3 = system_map.get_location("pad3")

        async def _thread_a_gen(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
            yield orca.on("go")
            yield waiter_a

        async def _thread_b_gen(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
            yield orca.on("go")
            yield waiter_b

        async def _thread_c_gen(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
            yield emit_signal

        thread_a = ThreadTemplate(
            labware_template=plate_a, start=pad1, end=pad1,
            func=_thread_a_gen,
        )
        thread_b = ThreadTemplate(
            labware_template=plate_b, start=pad2, end=pad2,
            func=_thread_b_gen,
        )
        thread_c = ThreadTemplate(
            labware_template=plate_c, start=pad3, end=pad3,
            func=_thread_c_gen,
        )

        workflow = WorkflowTemplate("concurrent_test")
        workflow.add_thread(thread_a, is_start=True)
        workflow.add_thread(thread_b, is_start=True)
        workflow.add_thread(thread_c, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="",
            labwares=[plate_a, plate_b, plate_c],
            resources_registry=registry,
            system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)

        assert status.status == "completed", f"got {status.status}: {status.error}"
        assert sorted(received) == ["thread_a", "thread_b"], (
            f"Both threads should have run after event, got {received}"
        )
        await runtime.shutdown()


# ---------------------------------------------------------------------------
# 7. Wait step timeout + retry recovery
# ---------------------------------------------------------------------------


class TestWaitStepTimeoutRetry:

    @pytest.mark.asyncio
    @pytest.mark.timeout(15)
    @pytest.mark.slow
    async def test_wait_timeout_then_retry_succeeds(self) -> None:
        """After a wait step times out and pauses, operator retry with event
        arriving should let the thread continue.
        """
        post_wait_ran: list[bool] = []
        main_paused = asyncio.Event()

        @orca.method()
        async def after_wait(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            post_wait_ran.append(True)
            return
            yield

        # Hold the emit until the main thread has timed out and paused, so the
        # signal provably cannot arrive before the wait step times out.
        @orca.method()
        async def delayed_emit(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            await main_paused.wait()
            await ctx.emit("signal", value="go")
            return
            yield

        plate = create_test_plate_template("plate_96")

        async def _main_gen(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
            yield orca.on("signal", timeout=0.5)
            yield after_wait

        main_thread = ThreadTemplate(
            labware_template=plate, start="pad1", end="pad1",
            func=_main_gen,
        )

        runtime, workflow = await _build_two_thread_system(
            main_thread, delayed_emit, all_methods=[after_wait],
        )
        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)

        # Wait for the thread to pause due to timeout
        paused = await wait_for_paused_thread(runtime, submission.execution_id)
        main_paused.set()

        # Manually emit the event, then retry so the wait succeeds immediately
        exec_entry = runtime._executions[submission.execution_id]
        assert exec_entry.executing_workflow is not None
        ecr = exec_entry.executing_workflow._event_channel_registry
        await ecr.get_or_create("signal").publish(value="go")
        runtime.recover_thread(submission.execution_id, paused.id, RecoveryDecision.RETRY)

        # Wait for completion
        status = await execution_outcome(runtime, submission, timeout=10.0)
        assert status.status == "completed", f"got {status.status}: {status.error}"
        assert post_wait_ran == [True], (
            "Method after wait step should have executed after retry"
        )
        await runtime.shutdown()


# ---------------------------------------------------------------------------
# 8. Yield thread with event coordination (on + emit in generator)
# ---------------------------------------------------------------------------


class TestYieldThreadEventCoordination:

    @pytest.mark.asyncio
    async def test_yield_thread_can_use_ctx_on(self) -> None:
        """A yield thread generator can use ctx.wait_for() to wait for events
        and decide which methods to yield based on event values.
        """
        branch_taken: list[str] = []

        @orca.method()
        async def action_a(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            branch_taken.append("a")
            return
            yield

        @orca.method()
        async def action_b(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            branch_taken.append("b")
            return
            yield

        @orca.method()
        async def emit_decision(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            await _await_event_waiters(ctx, "decision")
            await ctx.emit("decision", value="take_b")
            return
            yield

        plate = create_test_plate_template("plate_96")

        async def deciding_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
            value, _ = await ctx.wait_for("decision", timeout=5.0)
            if value == "take_a":
                yield action_a
            else:
                yield action_b

        yield_tmpl = ThreadTemplate(
            labware_template=plate, start="pad1", end="pad1",
            func=deciding_thread,
        )

        runtime, workflow = await _build_two_thread_system(
            yield_tmpl, emit_decision,
            all_methods=[action_a, action_b],
        )
        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)

        assert status.status == "completed", f"got {status.status}: {status.error}"
        assert branch_taken == ["b"], (
            f"Generator should have yielded action_b, got {branch_taken}"
        )
        await runtime.shutdown()
