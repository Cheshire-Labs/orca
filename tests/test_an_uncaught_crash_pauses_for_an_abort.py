"""A crash the thread's own error handling does not catch pauses the thread at once, offering ABORT_THREAD only.

The failed step is unknown, so nothing can be retried or carried past. A sibling entry thread keeps working
meanwhile, and ABORT_THREAD ends only the crashed thread. An author's failure_policy=ABORT still fails the
thread; a failed auto-spawn pauses it at SPAWN_CAPACITY, where RETRY spawns again.
"""
import asyncio
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.runtime.execution_phase import ExecutionPhase
from orca.config import OrcaConfig
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.actions.dynamic_resource_action import UnresolvedLocationAction
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import ActionTemplate
from orca.workflow_models.status_enums import FailurePolicy, RecoveryDecision
from orca.workflow_models.submission import Submission
from orca.workflow_models.thread_context import ThreadContext
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service, memory_unsaved_positions
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.test_helpers import (
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
    execution_outcome,
    wait_until,
    wire_system_map,
)


def _crash_when_starting(monkeypatch: pytest.MonkeyPatch, template_name: str) -> None:
    original = ExecutingLabwareThread.initialize_labware

    async def crash_for_one_template(self: ExecutingLabwareThread) -> None:
        if self.labware.template_name == template_name:
            raise RuntimeError(f"crash while starting {template_name}")
        await original(self)

    monkeypatch.setattr(ExecutingLabwareThread, "initialize_labware", crash_for_one_template)


async def _two_entry_threads(
    gate: asyncio.Event, failure_policy: FailurePolicy | None = None,
) -> tuple[SystemRuntime, WorkflowTemplate]:
    shakers = [create_test_device("shaker1"), create_test_device("shaker2")]
    transporter = create_test_transporter("robot1", ["shaker1", "shaker2", "pad1", "pad2"])
    working, broken = create_test_plate_template("working"), create_test_plate_template("broken")
    registry = ResourceRegistry()
    for device in shakers:
        registry.add_resource(device)
    registry.add_resource(transporter)
    pool1, pool2 = ResourcePool("shaker1", [shakers[0]]), ResourcePool("shaker2", [shakers[1]])
    registry.add_resource_pool(pool1)
    registry.add_resource_pool(pool2)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": shakers[0], "shaker2": shakers[1]}, pads=["pad1", "pad2"])

    @orca.action(device=pool1, inputs=[working])
    async def held_shake(ctx: ActionContext) -> None:
        await gate.wait()
        await ctx.device().shake(duration=1, speed=100)

    @orca.action(device=pool2, inputs=[broken], failure_policy=failure_policy)
    async def broken_shake(ctx: ActionContext) -> None:
        if failure_policy is FailurePolicy.ABORT:
            raise RuntimeError("the author's own code failed")
        await ctx.device().shake(duration=1, speed=200)

    @orca.method
    async def working_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield held_shake

    @orca.method
    async def broken_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield broken_shake

    pad1, pad2 = system_map.get_location("pad1"), system_map.get_location("pad2")

    @orca.thread(labware=working, start=pad1, end=pad1)
    async def working_journey(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield working_method

    @orca.thread(labware=broken, start=pad2, end=pad2)
    async def broken_journey(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield broken_method

    workflow = WorkflowTemplate("two_entries")
    workflow.add_thread(working_journey, is_start=True)
    workflow.add_thread(broken_journey, is_start=True)
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[working, broken], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=EventBus(), config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    runtime = await memory_runtime(builder.get_system())
    await runtime.start()
    return runtime, workflow


def _thread_named(runtime: SystemRuntime, submission: Submission, prefix: str) -> str:
    return next(t.id for t in runtime.list_threads(submission.execution_id) if t.name.startswith(prefix))


def _status(runtime: SystemRuntime, submission: Submission, prefix: str) -> str | None:
    return next((t.status for t in runtime.list_threads(submission.execution_id) if t.name.startswith(prefix)), None)


async def test_a_crash_at_start_pauses_at_once_for_an_abort_while_the_sibling_keeps_working(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _crash_when_starting(monkeypatch, "broken")
    gate = asyncio.Event()
    runtime, workflow = await _two_entry_threads(gate)
    try:
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.CHECK_RUN)
        await wait_until(
            lambda: any(t.status == "PAUSED" for t in runtime.list_threads(submission.execution_id)), timeout=10.0,
        )
        [paused] = [t for t in runtime.get_execution_detail(submission.execution_id).threads if t.status == "PAUSED"]

        assert paused.name.startswith("broken")
        assert "crash while starting broken" in (paused.last_error or "")
        assert paused.pause_site == "UNHANDLED_ERROR"
        assert list(paused.honoured_decisions) == ["ABORT_THREAD"]
        assert not gate.is_set() and _status(runtime, submission, "working") != "COMPLETED"
    finally:
        gate.set()
        await runtime.shutdown()


async def test_abort_thread_ends_only_the_crashed_thread_and_the_run_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    _crash_when_starting(monkeypatch, "broken")
    gate = asyncio.Event()
    runtime, workflow = await _two_entry_threads(gate)
    try:
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.CHECK_RUN)
        await wait_until(lambda: _status(runtime, submission, "broken") == "PAUSED", timeout=10.0)

        runtime.recover_thread(submission.execution_id, _thread_named(runtime, submission, "broken"), RecoveryDecision.ABORT_THREAD)
        gate.set()

        outcome = await execution_outcome(runtime, submission, timeout=20.0)
        assert _status(runtime, submission, "broken") == "ABORTED"
        assert _status(runtime, submission, "working") == "COMPLETED"
        assert outcome.status is ExecutionPhase.PARTIAL_COMPLETED, "the operator aborted one thread; the other completed"
    finally:
        gate.set()
        await runtime.shutdown()


async def test_a_failed_auto_spawn_pauses_the_thread_where_retry_spawns_again(monkeypatch: pytest.MonkeyPatch) -> None:
    async def spawn_fails(self: ExecutingLabwareThread, unresolved: UnresolvedLocationAction) -> None:
        raise RuntimeError("no template could be spawned")

    monkeypatch.setattr(ExecutingLabwareThread, "_auto_spawn_for_action", spawn_fails)
    gate = asyncio.Event()
    gate.set()
    runtime, workflow = await _two_entry_threads(gate)
    try:
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.CHECK_RUN)
        await wait_until(
            lambda: len(runtime.list_threads(submission.execution_id)) == 2 and all(
                t.status in ("FAILED", "COMPLETED", "ABORTED", "PAUSED") for t in runtime.list_threads(submission.execution_id)
            ),
            timeout=20.0,
        )
        threads = runtime.list_threads(submission.execution_id)
        assert {t.status for t in threads} == {"PAUSED"}
        assert {t.pause_site for t in threads} == {"SPAWN_CAPACITY"}
        assert all("RETRY" in t.honoured_decisions for t in threads)
    finally:
        await runtime.shutdown()


async def test_an_action_under_failure_policy_abort_still_fails_the_thread() -> None:
    gate = asyncio.Event()
    gate.set()
    runtime, workflow = await _two_entry_threads(gate, failure_policy=FailurePolicy.ABORT)
    try:
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.CHECK_RUN)
        await wait_until(lambda: _status(runtime, submission, "broken") in ("FAILED", "PAUSED", "ABORTED"), timeout=20.0)
        assert _status(runtime, submission, "broken") == "FAILED"
    finally:
        await runtime.shutdown()


async def test_aborting_the_run_ends_a_thread_paused_on_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    _crash_when_starting(monkeypatch, "broken")
    gate = asyncio.Event()
    runtime, workflow = await _two_entry_threads(gate)
    try:
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.CHECK_RUN)
        await wait_until(lambda: _status(runtime, submission, "broken") == "PAUSED", timeout=10.0)

        await runtime.abort_execution(submission.execution_id)

        await wait_until(lambda: _status(runtime, submission, "broken") in ("STOPPED", "ABORTED"), timeout=10.0)
    finally:
        gate.set()
        await runtime.shutdown()


async def test_an_abort_policy_action_that_handles_its_own_timeout_leaves_a_later_crash_to_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The author caught the error, so it never ended the thread by policy; a crash after it still pauses."""
    device = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate_96")
    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.ABORT)
    async def tolerant(ctx: ActionContext) -> None:
        try:
            await ctx.wait_for("never", timeout=0.05)
        except TimeoutError:
            pass
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def tolerant_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield tolerant

    pad1 = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad1, end=pad1)
    async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield tolerant_method

    workflow = WorkflowTemplate("tolerant")
    workflow.add_thread(plate_thread, is_start=True)
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[plate], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=EventBus(), config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()

    async def crash_at_the_end(self: ExecutingLabwareThread) -> None:
        raise RuntimeError("crash after the action")

    monkeypatch.setattr(ExecutingLabwareThread, "_handle_thread_completion", crash_at_the_end)
    runtime = await memory_runtime(builder.get_system())
    await runtime.start()
    try:
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.CHECK_RUN)
        await wait_until(
            lambda: any(t.status in ("PAUSED", "FAILED") for t in runtime.list_threads(submission.execution_id)),
            timeout=10.0,
        )
        assert [t.status for t in runtime.list_threads(submission.execution_id)] == ["PAUSED"]
    finally:
        await runtime.shutdown()
