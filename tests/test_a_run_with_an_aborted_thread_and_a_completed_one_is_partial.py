"""A run where the operator aborted some threads and the rest completed ends PARTIAL_COMPLETED.

`aborted` stays for a run the operator stopped, or one where nothing completed; `failed` still wins when a thread
failed.
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.config import OrcaConfig
from orca.events.event_bus import EventBus
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.sinks import CollectorSink
from orca.runtime.execution_phase import ExecutionPhase
from orca.runtime.system_runtime import ExecutionState, SystemRuntime, run_phase_of
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.status_enums import FailurePolicy, LabwareThreadStatus, RecoveryDecision
from orca.workflow_models.submission_modes import SubmissionStatus
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service, memory_unsaved_positions
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.mock import UniversalMockDevice
from tests.test_helpers import create_test_plate_template, create_test_transporter, wait_for_paused_threads, wire_system_map


class _ShakeFails(UniversalMockDevice):
    async def shake(self, duration: int, speed: int) -> None:
        raise RuntimeError("the shaker jammed")


async def _one_failing_and_one_working_thread() -> tuple[SystemRuntime, WorkflowTemplate]:
    failing = _ShakeFails("failing")
    working = UniversalMockDevice("working")
    transporter = create_test_transporter("robot1", ["failing", "working", "pad1", "pad2"])
    plate_a = create_test_plate_template("plate_a")
    plate_b = create_test_plate_template("plate_b")
    registry = ResourceRegistry()
    for resource in (failing, working, transporter):
        registry.add_resource(resource)
    failing_pool = ResourcePool("failing", [failing])
    working_pool = ResourcePool("working", [working])
    registry.add_resource_pool(failing_pool)
    registry.add_resource_pool(working_pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"failing": failing, "working": working}, pads=["pad1", "pad2"])

    @orca.action(device=failing_pool, inputs=[plate_a], failure_policy=FailurePolicy.PAUSE)
    async def shake_a(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.action(device=working_pool, inputs=[plate_b])
    async def shake_b(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def method_a(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_a

    @orca.method
    async def method_b(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_b

    pad1 = system_map.get_location("pad1")
    pad2 = system_map.get_location("pad2")

    @orca.thread(labware=plate_a, start=pad1, end=pad1)
    async def thread_a(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield method_a

    @orca.thread(labware=plate_b, start=pad2, end=pad2)
    async def thread_b(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield method_b

    @orca.workflow(name="one_of_two_aborted")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(thread_a)
        wf.start(thread_b)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="partial", description="", labwares=[plate_a, plate_b], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=event_bus, config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    return await memory_runtime(builder.get_system(), event_bus=event_bus), workflow


async def test_an_operator_abort_of_one_thread_while_the_other_completes_ends_partial_completed() -> None:
    runtime, workflow = await _one_failing_and_one_working_thread()
    events = CollectorSink()
    runtime.register_sink(events)
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.CHECK_RUN)
    try:
        [paused] = await wait_for_paused_threads(runtime, record.id)
        runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_THREAD)
        final = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)

        assert sorted(t.status for t in runtime.list_threads(record.id)) == ["ABORTED", "COMPLETED"]
        assert final.status is ExecutionState.PARTIAL_COMPLETED
        assert [s.status for s in runtime._executions[record.id].submissions] == [SubmissionStatus.PARTIAL_COMPLETED]
        assert f"EXECUTION.{record.id}.PARTIAL_COMPLETED" in [e.event_name for e in events.events]
    finally:
        await runtime.shutdown()


_C, _A, _S, _F = (
    LabwareThreadStatus.COMPLETED, LabwareThreadStatus.ABORTED, LabwareThreadStatus.STOPPED, LabwareThreadStatus.FAILED,
)


@pytest.mark.parametrize("statuses, phase", [
    ([_C, _C], ExecutionPhase.COMPLETED),
    ([_A, _C], ExecutionPhase.PARTIAL_COMPLETED),
    ([_A, _A], ExecutionPhase.ABORTED),
    ([_S, _A, _C], ExecutionPhase.ABORTED),
    ([_F, _A, _C], ExecutionPhase.FAILED),
    ([_F, _S], ExecutionPhase.FAILED),
])
def test_the_run_phase_follows_the_rules_in_order(
    statuses: list[LabwareThreadStatus], phase: ExecutionPhase,
) -> None:
    assert run_phase_of(statuses) is phase
