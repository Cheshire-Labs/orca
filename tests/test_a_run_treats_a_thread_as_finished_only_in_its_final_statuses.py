"""The runtime reads a thread as finished only when it is completed, stopped, aborted or failed.

Each test holds a real one-thread run with its thread at a chosen status, then asks the
runtime what it does with that thread. Statuses the run cannot be held at without an
operator or a device fault (such as awaiting a reservation) are not covered here.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

import pytest

import orca.orca as orca
from orca.config import OrcaConfig
from orca.events.event_bus import EventBus
from orca.resource_models.labware import PlateTemplate
from orca.resource_models.resource_pool import ResourcePool
from orca.workflow_models.labware_group import LabwareGroup, LabwareGroupMember
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import ThreadTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate, MethodTemplate
from orca.workflow_models.status_enums import FailurePolicy, LabwareThreadStatus, RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.memory_activity import memory_unsaved_positions
from tests.mock import UniversalMockDevice
from tests.test_helpers import create_test_transporter, wait_until, wire_system_map
from tests.memory_activity import memory_labware_service

S = LabwareThreadStatus

FINISHED = frozenset({S.COMPLETED, S.STOPPED, S.ABORTED, S.FAILED})
HELD_STATUSES = [S.EXECUTING_ACTION, S.STOPPING, S.COMPLETED, S.STOPPED, S.FAILED, S.ABORTED]


class _GatedShaker(UniversalMockDevice):
    """A shaker whose shake waits for the test, then succeeds or jams."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.in_shake = asyncio.Event()
        self.release = asyncio.Event()
        self.jams = False

    async def shake(self, duration: int, speed: int) -> None:
        self.in_shake.set()
        await self.release.wait()
        if self.jams:
            raise RuntimeError("shaker jammed")
        await super().shake(duration, speed)


@dataclass(frozen=True)
class _HeldRun:
    runtime: SystemRuntime
    execution_id: str
    thread: ExecutingLabwareThread


async def _start_a_run_that_waits_in_its_shake(
    failure_policy: FailurePolicy,
) -> tuple[SystemRuntime, _GatedShaker, str]:
    device = _GatedShaker("station")
    transporter = create_test_transporter("robot1", ["start_pad", "station", "waste"])
    plate = PlateTemplate("owner", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("station", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"station": device}, pads=["start_pad", "waste"])

    @orca.action(device=pool, inputs=[plate], failure_policy=failure_policy)
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    async def _method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_action
    shake_method = MethodTemplate("shake_method", func=_method)

    async def _thread_func(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield shake_method
    owner = ThreadTemplate(
        labware_template=plate,
        start=system_map.get_location("start_pad"),
        end=system_map.get_location("waste"),
        func=_thread_func,
    )
    workflow = WorkflowTemplate("one_shake")
    workflow.add_thread(owner, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="one_shake_system", description="", labwares=[plate],
        resources_registry=registry, system_map=system_map, workflows=[workflow],
        event_bus=event_bus, config=OrcaConfig(), ops_history_store=memory_ops_store(),
        variable_store=memory_variable_store(), labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(),
        unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    runtime = await memory_runtime(builder.get_system(), event_bus=event_bus)
    await runtime.start()
    group = LabwareGroup(
        id=str(uuid4()), members=(LabwareGroupMember(thread_template_name="owner"),),
    )
    submission = await runtime.submit(workflow, groups=[group], mode=WorkflowRunMode.CHECK_RUN)
    await asyncio.wait_for(device.in_shake.wait(), timeout=10.0)
    return runtime, device, submission.execution_id


async def _reach(status: LabwareThreadStatus) -> tuple[_HeldRun, _GatedShaker]:
    fails = status in (S.FAILED, S.ABORTED)
    policy = FailurePolicy.PAUSE if status is S.ABORTED else FailurePolicy.ABORT
    runtime, device, execution_id = await _start_a_run_that_waits_in_its_shake(policy)
    thread = runtime.system.executing_threads[0]
    device.jams = fails
    if status is S.STOPPING or status is S.STOPPED:
        thread.stop()
    if status is not S.EXECUTING_ACTION and status is not S.STOPPING:
        device.release.set()
    if status is S.ABORTED:
        await wait_until(lambda: thread.status is S.PAUSED, timeout=10.0)
        runtime.recover_thread(execution_id, thread.id, RecoveryDecision.ABORT_THREAD)
    await wait_until(lambda: thread.status is status, timeout=10.0)
    return _HeldRun(runtime, execution_id, thread), device


@asynccontextmanager
async def _run_held_at(status: LabwareThreadStatus) -> AsyncIterator[_HeldRun]:
    held, device = await _reach(status)
    try:
        yield held
    finally:
        device.release.set()
        await held.runtime.shutdown()


@pytest.mark.parametrize("status", HELD_STATUSES, ids=lambda s: s.name)
async def test_pausing_a_run_skips_a_thread_only_when_it_has_finished(
    status: LabwareThreadStatus,
) -> None:
    async with _run_held_at(status) as held:
        counts = held.runtime.pause_all_threads(held.execution_id)

        assert counts["terminal_skipped"] == (1 if status in FINISHED else 0)
        assert counts["pausing"] == (0 if status in FINISHED else 1)


@pytest.mark.parametrize("status", HELD_STATUSES, ids=lambda s: s.name)
async def test_resuming_a_run_skips_a_thread_only_when_it_has_finished(
    status: LabwareThreadStatus,
) -> None:
    async with _run_held_at(status) as held:
        counts = held.runtime.resume_all_threads(held.execution_id)

        assert counts["completed_skipped"] == (1 if status in FINISHED else 0)


@pytest.mark.parametrize("status", HELD_STATUSES, ids=lambda s: s.name)
async def test_a_mutation_waiting_for_a_pause_is_refused_only_for_a_finished_thread(
    status: LabwareThreadStatus,
) -> None:
    async with _run_held_at(status) as held:
        def mutation(system: ISystem, thread_id: str) -> None:
            return None

        if status in FINISHED:
            with pytest.raises(ValueError, match="cannot mutate"):
                held.runtime.mutate_on_next_pause(held.execution_id, held.thread.id, mutation)
        else:
            held.runtime.mutate_on_next_pause(held.execution_id, held.thread.id, mutation)


@pytest.mark.parametrize("status", HELD_STATUSES, ids=lambda s: s.name)
async def test_the_execution_detail_counts_a_thread_as_active_unless_finished_or_stopping(
    status: LabwareThreadStatus,
) -> None:
    async with _run_held_at(status) as held:
        detail = held.runtime.get_execution_detail(held.execution_id)

        assert [t.status for t in detail.threads] == [status.name]
        assert detail.active_thread_count == (1 if status is S.EXECUTING_ACTION else 0)
