"""An execution must not report COMPLETED while it holds an ABORTED or STOPPED
thread.

The execution phase was derived purely from the workflow task's outcome: a
clean task return became COMPLETED. But an operator-aborted thread (ABORT_THREAD)
and a stopped thread both return cleanly -- the task does not raise -- so an
execution with an aborted thread was mislabeled COMPLETED. The phase must be a
roll-up of the per-thread terminal states: COMPLETED only if every thread
COMPLETED; ABORTED if any thread ended ABORTED or STOPPED.
"""

import asyncio
from collections.abc import AsyncGenerator

import orca.orca as orca
from orca.events.event_bus import EventBus
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.status_enums import (
    FailurePolicy,
    RecoveryDecision,
    WorkflowStatus,
)
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext

from tests.mock import UniversalMockDevice
from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    wait_for_paused_threads,
    wire_system_map,
)


class _FailOnceDevice(UniversalMockDevice):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.should_fail = True

    async def shake(self, duration: int, speed: int) -> None:
        if self.should_fail:
            raise RuntimeError("Simulated shake failure")
        await super().shake(duration, speed)


async def _build_single_thread_system() -> tuple[SystemRuntime, WorkflowTemplate, _FailOnceDevice]:
    device = _FailOnceDevice("dev1")
    transporter = create_test_transporter("robot1", ["dev1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("dev1", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(
        system_map, devices={"dev1": device}, pads=["pad1"],
    )

    @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.PAUSE)
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shake_action

    pad = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad, end=pad)
    async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield shake_method

    @orca.workflow(name="single_thread_wf")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system",
        description="",
        labwares=[plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    runtime = SystemRuntime(builder.get_system(), event_bus=event_bus)
    return runtime, workflow, device


async def test_execution_not_completed_when_thread_aborted() -> None:
    runtime, workflow, device = await _build_single_thread_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        await wait_for_paused_threads(runtime, record.id)
        paused = runtime.get_paused_threads(record.id)
        assert len(paused) == 1
        runtime.recover_thread(record.id, paused[0].id, RecoveryDecision.ABORT_THREAD)

        final = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
        statuses = [t.status for t in runtime.list_threads(record.id)]
        assert statuses == ["ABORTED"], f"expected the thread ABORTED; got {statuses}"
        assert final.status != ExecutionState.COMPLETED, (
            "an execution holding an ABORTED thread must not report COMPLETED; "
            f"got {final.status} with thread statuses {statuses}."
        )
        assert final.status == ExecutionState.ABORTED

        # The internal ExecutingWorkflow.status must agree with the phase
        # roll-up: a run holding an ABORTED thread is not COMPLETED. Before the
        # alignment it stayed COMPLETED (the check only saw PAUSED-with-error),
        # disagreeing with execution.phase=ABORTED.
        exec_entry = runtime._executions[record.id]
        assert exec_entry.executing_workflow is not None
        assert exec_entry.executing_workflow.status == WorkflowStatus.ERRORED, (
            "executing_workflow.status must roll up to ERRORED when a thread "
            f"aborted; got {exec_entry.executing_workflow.status}."
        )
    finally:
        await runtime.shutdown()
