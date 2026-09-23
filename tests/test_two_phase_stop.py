"""Item 5 (Decision C): pause is immediate; stop is a two-call confirmed abort.

Pins the locked behavior:
- (a) the first stop call pauses immediately + arms, and does NOT abort;
- (b) a second confirmed call aborts an armed, paused execution;
- (c) a cold confirmed call (never armed) pauses + arms, does not abort;
- (d) resume disarms, so a later confirm re-arms instead of aborting;
- (e) a JOIN_EXISTING submission into a paused execution is rejected;
- (f) the confirmed-abort response reports the terminal ABORTED phase, not a
      transient STOPPING (the pre-existing status race).

A thread inside a long device action only reaches PAUSED at its next safe
point, so the pause assertions are on the execution-level latch + phase, not on
the per-thread state -- the documented "freeze at safe point, not mid-action"
caveat.
"""

import asyncio
from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest

import orca.orca as orca
from orca.events.event_bus import EventBus
from orca.resource_models.labware import PlateTemplate
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.execution import ExecutionPhase
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.runtime_interface import SubmissionToPausedExecutionError
from orca.runtime.sinks import CollectorSink
from orca.runtime.submission import BatchMode, SubmissionStatus
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import ThreadTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate, MethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from tests.mock import UniversalMockDevice
from tests.test_helpers import create_test_transporter, wire_system_map


class _HangingDevice(UniversalMockDevice):
    """Shake blocks until ``release`` is set, exposing a long-running action so
    the execution stays ACCEPTING while the test pauses/stops it."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.in_shake = asyncio.Event()
        self.release = asyncio.Event()

    async def shake(self, duration: int, speed: int) -> None:
        self.in_shake.set()
        await self.release.wait()
        await super().shake(duration, speed)


def _owner_group() -> LabwareGroup:
    return LabwareGroup(
        id=str(uuid4()),
        members=(LabwareGroupMember(thread_template_name="owner"),),
    )


async def _build_hanging_owner_system() -> tuple[SystemRuntime, WorkflowTemplate, _HangingDevice, EventBus]:
    """One entry thread ("owner") that runs a single hanging shake action."""
    device = _HangingDevice("station")
    transporter = create_test_transporter("robot1", ["start_pad", "station", "waste"])
    owner_plate = PlateTemplate("owner", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    station_pool = ResourcePool("station", [device])
    registry.add_resource_pool(station_pool)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map, devices={"station": device}, pads=["start_pad", "waste"]
    )

    @orca.action(device=station_pool, inputs=[owner_plate])
    async def hang_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    async def _method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield hang_action
    hang_method = MethodTemplate("hang_method", func=_method)

    start_pad = system_map.get_location("start_pad")
    waste = system_map.get_location("waste")

    async def _owner_func(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield hang_method
    owner_thread = ThreadTemplate(
        labware_template=owner_plate, start=start_pad, end=waste, func=_owner_func,
    )

    workflow = WorkflowTemplate("hang_owner_wf")
    workflow.add_thread(owner_thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="hang_owner_system",
        description="",
        labwares=[owner_plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    runtime = SystemRuntime(system, event_bus=event_bus)
    return runtime, workflow, device, event_bus


async def _submit_and_run_to_hang(
    runtime: SystemRuntime, workflow: WorkflowTemplate, device: _HangingDevice,
) -> str:
    await runtime.start()
    submission = await runtime.submit(
        workflow, groups=[_owner_group()], mode=WorkflowRunMode.PURE_SIM,
    )
    await asyncio.wait_for(device.in_shake.wait(), timeout=10.0)
    return submission.execution_id


async def test_first_stop_pauses_immediately_and_arms_without_aborting() -> None:
    runtime, workflow, device, _bus = await _build_hanging_owner_system()
    eid = await _submit_and_run_to_hang(runtime, workflow, device)
    try:
        outcome = await runtime.stop_execution(eid)

        assert outcome.armed is True
        assert outcome.aborted is False
        # The execution is paused but NOT terminal -- still conceptually
        # ACCEPTING; the latch is set and the abort is armed.
        assert runtime.get_execution_status(eid).status is ExecutionPhase.ACCEPTING
        execution = runtime.require_execution(eid)
        assert execution.paused_at is not None
        assert execution.abort_armed is True
    finally:
        device.release.set()
        await runtime.shutdown()


async def test_second_confirmed_stop_aborts_and_reports_terminal_phase() -> None:
    runtime, workflow, device, _bus = await _build_hanging_owner_system()
    eid = await _submit_and_run_to_hang(runtime, workflow, device)
    try:
        armed = await runtime.stop_execution(eid)
        assert armed.armed is True

        aborted = await runtime.stop_execution(eid, confirm=True)
        assert aborted.aborted is True
        # (f) the response carries the accurate terminal phase, not STOPPING.
        assert aborted.phase is ExecutionPhase.ABORTED

        final = await runtime.wait(eid)
        assert final.status == ExecutionState.ABORTED
    finally:
        device.release.set()
        await runtime.shutdown()


async def test_cold_confirmed_stop_pauses_and_arms_does_not_abort() -> None:
    runtime, workflow, device, _bus = await _build_hanging_owner_system()
    eid = await _submit_and_run_to_hang(runtime, workflow, device)
    try:
        # A single confirmed call on a never-armed execution must NOT abort --
        # two distinct intentful calls are always required.
        outcome = await runtime.stop_execution(eid, confirm=True)

        assert outcome.armed is True
        assert outcome.aborted is False
        assert runtime.get_execution_status(eid).status is ExecutionPhase.ACCEPTING
        assert runtime.require_execution(eid).abort_armed is True
    finally:
        device.release.set()
        await runtime.shutdown()


async def test_resume_disarms_so_later_confirm_does_not_abort() -> None:
    runtime, workflow, device, _bus = await _build_hanging_owner_system()
    eid = await _submit_and_run_to_hang(runtime, workflow, device)
    try:
        await runtime.stop_execution(eid)  # pause + arm
        runtime.resume_execution(eid)

        execution = runtime.require_execution(eid)
        assert execution.paused_at is None
        assert execution.abort_armed is False

        # A confirm after resume re-arms (treated as call 1); it must NOT abort
        # the now-running execution.
        outcome = await runtime.stop_execution(eid, confirm=True)
        assert outcome.aborted is False
        assert outcome.armed is True
        assert runtime.get_execution_status(eid).status is ExecutionPhase.ACCEPTING
    finally:
        device.release.set()
        await runtime.shutdown()


async def test_stop_on_terminal_execution_is_noop() -> None:
    runtime, workflow, device, _bus = await _build_hanging_owner_system()
    eid = await _submit_and_run_to_hang(runtime, workflow, device)
    try:
        # Let the run complete, then stop: nothing to arm or abort.
        device.release.set()
        final = await runtime.wait(eid)
        assert final.status == ExecutionState.COMPLETED

        outcome = await runtime.stop_execution(eid, confirm=True)
        assert outcome.armed is False
        assert outcome.aborted is False
        assert outcome.phase is ExecutionPhase.COMPLETED
    finally:
        await runtime.shutdown()


async def test_abort_emits_aborted_events_and_status() -> None:
    # A started-then-stopped run is an abort, not a cancel: every operator-facing
    # channel says ABORTED. No EXECUTION/SUBMISSION event ever carries CANCELLED.
    runtime, workflow, device, event_bus = await _build_hanging_owner_system()
    emitted: list[str] = []
    event_bus.subscribe_all(lambda name, _ctx: emitted.append(name))
    eid = await _submit_and_run_to_hang(runtime, workflow, device)
    try:
        await runtime.stop_execution(eid)
        await runtime.stop_execution(eid, confirm=True)
        final = await runtime.wait(eid)
        assert final.status == ExecutionState.ABORTED

        submission = runtime.require_execution(eid).submissions[0]
        assert submission.status is SubmissionStatus.ABORTED

        assert f"EXECUTION.{eid}.ABORTED" in emitted
        assert f"SUBMISSION.{submission.id}.ABORTED" in emitted
        assert not any(name.endswith(".CANCELLED") for name in emitted)
    finally:
        device.release.set()
        await runtime.shutdown()


async def test_abort_terminal_events_reach_registered_sink() -> None:
    """A registered sink must receive the terminal EXECUTION/SUBMISSION ABORTED
    events, not just the workflow EventBus. The sink sits on the SystemEventBus
    behind the forwarder, which used to drop the execution before _on_task_done
    emitted; the COMPLETED-only sink guard would not catch an abort-path leak."""
    runtime, workflow, device, _bus = await _build_hanging_owner_system()
    collector = CollectorSink()
    runtime.register_sink(collector)
    eid = await _submit_and_run_to_hang(runtime, workflow, device)
    try:
        await runtime.stop_execution(eid)
        await runtime.stop_execution(eid, confirm=True)
        final = await runtime.wait(eid)
        assert final.status == ExecutionState.ABORTED

        submission = runtime.require_execution(eid).submissions[0]
        names = [e.event_name for e in collector.events]
        assert f"EXECUTION.{eid}.ABORTED" in names, (
            f"terminal ABORTED execution event missing from sink; got: {sorted(set(names))}"
        )
        assert f"SUBMISSION.{submission.id}.ABORTED" in names
    finally:
        device.release.set()
        await runtime.shutdown()


async def test_join_existing_into_paused_execution_is_rejected() -> None:
    runtime, workflow, device, _bus = await _build_hanging_owner_system()
    eid = await _submit_and_run_to_hang(runtime, workflow, device)
    try:
        await runtime.stop_execution(eid)  # pause + arm

        with pytest.raises(SubmissionToPausedExecutionError) as excinfo:
            await runtime.submit(
                workflow, groups=[_owner_group()],
                mode=WorkflowRunMode.PURE_SIM,
                batch_mode=BatchMode.JOIN_EXISTING,
            )
        assert "paused" in str(excinfo.value).lower()
        assert excinfo.value.blocking_execution_id == eid
    finally:
        device.release.set()
        await runtime.shutdown()
