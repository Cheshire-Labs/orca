"""A confirmed manual step honours a pause the operator asked for first.

``ctx.manual_step`` parks the thread on an await inside the action body, with
the thread still EXECUTING_ACTION. Without a checkpoint there, an operator who
pauses the execution and then confirms the step watches the rest of the body
run on real hardware: the instruction was answered, so the await returns, and
the pause is not consulted again until the action finishes. A manual step is
the safest point the thread ever occupies, so the pause is honoured there.
"""

import asyncio
from typing import AsyncGenerator

import pytest

import orca.orca as orca
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.resource_models.labware import LabwareTemplate
from orca.resource_models.resource_pool import ResourcePool
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.mutation.errors import ThreadNotPausedError
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate, MethodTemplate
from orca.workflow_models.mutation_position import AtTail
from orca.workflow_models.status_enums import LabwareThreadStatus
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import ThreadTemplate
from tests.mock import UniversalMockDevice
from tests.mutation_helpers import wait_for_threads
from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    wait_for_paused_threads,
    wait_for_runtime_condition,
    wire_system_map,
)

INSTRUCTION = "swap the reagent trough, then confirm"


class _ShakeRecordingDevice(UniversalMockDevice):
    """Records every shake that actually reached the device."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.shakes: list[int] = []

    async def shake(self, duration: int, speed: int) -> None:
        self.shakes.append(speed)
        await super().shake(duration=duration, speed=speed)


class _Fixture:
    def __init__(
        self,
        runtime: SystemRuntime,
        workflow: WorkflowTemplate,
        device: _ShakeRecordingDevice,
        pool: ResourcePool,
        plate: LabwareTemplate,
    ) -> None:
        self.runtime = runtime
        self.workflow = workflow
        self.device = device
        self.pool = pool
        self.plate = plate


async def _build_manual_step_system() -> _Fixture:
    device = _ShakeRecordingDevice("device1")
    transporter = create_test_transporter("robot1", ["device1", "pad1"])
    plate: LabwareTemplate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("device1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate])
    async def manual_step_then_shake(ctx: ActionContext) -> None:
        await ctx.manual_step(INSTRUCTION)
        await ctx.device().shake(duration=1, speed=500)

    async def _method_gen(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield manual_step_then_shake

    method = MethodTemplate("manual_then_shake", func=_method_gen)
    pad_loc = system_map.get_location("pad1")

    async def _thread_gen(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield method

    thread = ThreadTemplate(
        labware_template=plate, start=pad_loc, end=pad_loc, func=_thread_gen,
    )
    workflow = WorkflowTemplate("manual_step_workflow")
    workflow.add_thread(thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[plate],
        resources_registry=registry, system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    runtime = SystemRuntime(builder.get_system(), event_bus=event_bus)
    return _Fixture(runtime, workflow, device, pool, plate)


async def _await_pending_step(runtime: SystemRuntime, execution_id: str) -> str:
    await wait_for_runtime_condition(
        runtime,
        lambda: bool(runtime.list_pending_manual_steps(execution_id)),
        timeout=10.0,
        message="manual step never announced",
    )
    return runtime.list_pending_manual_steps(execution_id)[0].step_id


@pytest.mark.timeout(60)
async def test_confirmed_manual_step_holds_the_body_until_resume() -> None:
    """Pause, then confirm: the rest of the action body waits for resume."""
    f = await _build_manual_step_system()
    await f.runtime.start()
    record = await f.runtime.submit_workflow(
        f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
    )
    await wait_for_threads(f.runtime, record.id)
    step_id = await _await_pending_step(f.runtime, record.id)

    f.runtime.pause_execution(record.id)
    await f.runtime.confirm_manual_step(record.id, step_id)

    paused = await wait_for_paused_threads(f.runtime, record.id, count=1, timeout=10.0)
    assert f.device.shakes == [], (
        "the operator asked the run to stop; the shake after the manual step "
        "must not have reached the device"
    )
    assert paused[0].pause_message, "a held thread must say why it is not moving"

    f.runtime.resume_execution(record.id)
    status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=30.0)

    assert status.status == ExecutionState.COMPLETED
    assert f.device.shakes == [500], "resume must release the rest of the body"
    await f.runtime.shutdown()


@pytest.mark.timeout(60)
async def test_unpaused_manual_step_confirm_runs_straight_on() -> None:
    """No pause pending: confirm returns into the body with no operator resume."""
    f = await _build_manual_step_system()
    await f.runtime.start()
    record = await f.runtime.submit_workflow(
        f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
    )
    await wait_for_threads(f.runtime, record.id)
    step_id = await _await_pending_step(f.runtime, record.id)

    await f.runtime.confirm_manual_step(record.id, step_id)
    status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=30.0)

    assert status.status == ExecutionState.COMPLETED
    assert f.device.shakes == [500]
    await f.runtime.shutdown()


@pytest.mark.timeout(60)
async def test_thread_held_after_confirm_is_visibly_paused() -> None:
    """The held thread reports PAUSED, not a silent stall inside the action."""
    f = await _build_manual_step_system()
    await f.runtime.start()
    record = await f.runtime.submit_workflow(
        f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
    )
    threads = await wait_for_threads(f.runtime, record.id)
    step_id = await _await_pending_step(f.runtime, record.id)

    f.runtime.pause_execution(record.id)
    await f.runtime.confirm_manual_step(record.id, step_id)
    await wait_for_paused_threads(f.runtime, record.id, count=1, timeout=10.0)

    held = [t.id for t in f.runtime.get_paused_threads(record.id)]
    assert held == [threads[0].id]
    detail = f.runtime.get_thread_detail(record.id, threads[0].id)
    assert detail.status == LabwareThreadStatus.PAUSED.name
    assert detail.last_error is None, "a held thread is not an errored thread"
    assert detail.pause_message is not None
    assert "manual step" in detail.pause_message, (
        f"the hold must name what it is waiting on: {detail.pause_message!r}"
    )
    assert f.runtime.list_pending_manual_steps(record.id) == [], (
        "the step was confirmed; it must not still read as pending while held"
    )

    f.runtime.resume_execution(record.id)
    await asyncio.wait_for(f.runtime.wait(record.id), timeout=30.0)
    await f.runtime.shutdown()


@pytest.mark.timeout(60)
async def test_abort_reaches_a_thread_held_after_confirm() -> None:
    """The hold does not swallow the operator's abort."""
    f = await _build_manual_step_system()
    await f.runtime.start()
    record = await f.runtime.submit_workflow(
        f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
    )
    await wait_for_threads(f.runtime, record.id)
    step_id = await _await_pending_step(f.runtime, record.id)

    await f.runtime.stop_execution(record.id)
    await f.runtime.confirm_manual_step(record.id, step_id)
    await wait_for_paused_threads(f.runtime, record.id, count=1, timeout=10.0)

    outcome = await asyncio.wait_for(
        f.runtime.stop_execution(record.id, confirm=True), timeout=30.0,
    )

    assert outcome.aborted
    assert f.device.shakes == []
    await f.runtime.shutdown()


@pytest.mark.timeout(60)
async def test_held_thread_accepts_a_mutation_the_running_body_refuses() -> None:
    """The hold is a real PAUSED thread, so mutation reaches it.

    A thread inside an action body is EXECUTING_ACTION and every mutation is
    refused. Pausing before the confirm gives the operator a point where the
    thread is parked at the instrument and can still be corrected.
    """
    f = await _build_manual_step_system()
    await f.runtime.start()
    record = await f.runtime.submit_workflow(
        f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
    )
    threads = await wait_for_threads(f.runtime, record.id)
    thread_id = threads[0].id
    step_id = await _await_pending_step(f.runtime, record.id)

    @orca.action(device=f.pool, inputs=[f.plate])
    async def corrective_shake(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=250)

    with pytest.raises(ThreadNotPausedError):
        f.runtime.system.insert_action(thread_id, corrective_shake, where=AtTail())

    f.runtime.pause_execution(record.id)
    await f.runtime.confirm_manual_step(record.id, step_id)
    await wait_for_paused_threads(f.runtime, record.id, count=1, timeout=10.0)

    f.runtime.system.insert_action(thread_id, corrective_shake, where=AtTail())

    f.runtime.resume_execution(record.id)
    status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=30.0)

    assert status.status == ExecutionState.COMPLETED
    assert f.device.shakes == [500, 250], (
        "the held body finishes, then the inserted correction runs"
    )
    await f.runtime.shutdown()


@pytest.mark.timeout(60)
async def test_held_thread_can_have_its_method_aborted() -> None:
    """Pause then confirm is also an exit: the rest of the body never runs.

    Aborting the method cancels the body where it is held, so the thread
    leaves the action without the operator having to stop the whole run.
    """
    f = await _build_manual_step_system()
    await f.runtime.start()
    record = await f.runtime.submit_workflow(
        f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
    )
    threads = await wait_for_threads(f.runtime, record.id)
    thread_id = threads[0].id
    step_id = await _await_pending_step(f.runtime, record.id)

    f.runtime.pause_execution(record.id)
    await f.runtime.confirm_manual_step(record.id, step_id)
    await wait_for_paused_threads(f.runtime, record.id, count=1, timeout=10.0)

    await f.runtime.system.abort_method(thread_id, method_name="manual_then_shake")
    f.runtime.resume_execution(record.id)
    status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=30.0)

    assert status.status == ExecutionState.COMPLETED
    assert f.device.shakes == [], "the aborted body's remaining device call must not run"
    assert f.runtime.get_thread_detail(record.id, thread_id).status == "COMPLETED"
    await f.runtime.shutdown()
