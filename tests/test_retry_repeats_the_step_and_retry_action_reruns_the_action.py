"""RETRY repeats the step that failed. RETRY_ACTION re-runs the whole action.

An action is the author's Python function. When a device command inside it
fails, that command can be repeated, so RETRY applies and RETRY_ACTION is
refused. When the author's own Python fails, no command failed, so the only
thing that can be repeated is the function: RETRY_ACTION applies and RETRY is
refused.
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.config import OrcaConfig
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.status_enums import FailurePolicy, RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext

from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service, memory_unsaved_positions
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    wait_for_paused_thread,
    wire_system_map,
)
from tests.test_op_level_device_retry import (
    _build_non_device_fail_system,
    _build_two_op_system,
)
from tests.mock import UniversalMockDevice


class _CountingShaker(UniversalMockDevice):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.shakes = 0

    async def shake(self, duration: int, speed: int) -> None:
        self.shakes += 1
        await super().shake(duration, speed)


async def _build_action_whose_python_fails_once() -> tuple[SystemRuntime, WorkflowTemplate, _CountingShaker]:
    """The action shakes, then its own Python raises on the first pass only."""
    device = _CountingShaker("dev1")
    transporter = create_test_transporter("robot1", ["dev1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("dev1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"dev1": device}, pads=["pad1"])

    passes: list[int] = []

    @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.PAUSE)
    async def shake_then_divide(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)
        passes.append(1)
        if len(passes) == 1:
            raise ZeroDivisionError("division by zero in the action's own code")

    async def _method_gen(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shake_then_divide

    method = MethodTemplate("shake_then_divide_method", func=_method_gen)
    pad = system_map.get_location("pad1")

    async def _thread_gen(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        del ctx
        yield method

    thread = ThreadTemplate(labware_template=plate, start=pad, end=pad, func=_thread_gen)
    workflow = WorkflowTemplate("python_fails_once_workflow")
    workflow.add_thread(thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[plate],
        resources_registry=registry, system_map=system_map,
        workflows=[workflow], event_bus=event_bus, config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    runtime = await memory_runtime(builder.get_system(), event_bus=event_bus)
    return runtime, workflow, device


async def test_retry_at_a_failed_device_command_repeats_only_that_command() -> None:
    runtime, workflow, device = await _build_two_op_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.CHECK_RUN)
    try:
        paused = await wait_for_paused_thread(runtime, record.id)
        assert (device.op1_calls, device.op2_calls) == (1, 1)

        runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.COMPLETED
        assert device.op1_calls == 1, "the command that succeeded must not run again"
        assert device.op2_calls == 2
    finally:
        await runtime.shutdown()


async def test_retry_action_at_a_failed_device_command_is_refused() -> None:
    runtime, workflow, device = await _build_two_op_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.CHECK_RUN)
    try:
        paused = await wait_for_paused_thread(runtime, record.id)

        with pytest.raises(ValueError, match="does not honour RETRY_ACTION") as refused:
            runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY_ACTION)
        assert "RETRY repeats" in str(refused.value)

        runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED
        assert device.op1_calls == 1
    finally:
        await runtime.shutdown()


async def test_retry_where_the_actions_own_python_failed_is_refused() -> None:
    runtime, workflow, _ = await _build_non_device_fail_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.CHECK_RUN)
    try:
        paused = await wait_for_paused_thread(runtime, record.id)

        with pytest.raises(ValueError, match="does not honour RETRY there") as refused:
            runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY)
        assert "no command failed" in str(refused.value)
        assert "use RETRY_ACTION" in str(refused.value)

        runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_THREAD)
        await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
    finally:
        await runtime.shutdown()


async def test_retry_action_reruns_an_action_whose_own_python_failed() -> None:
    runtime, workflow, device = await _build_action_whose_python_fails_once()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.CHECK_RUN)
    try:
        paused = await wait_for_paused_thread(runtime, record.id)
        assert device.shakes == 1

        runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY_ACTION)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.COMPLETED
        assert device.shakes == 2, "the action runs again from the top, its shake included"
    finally:
        await runtime.shutdown()
