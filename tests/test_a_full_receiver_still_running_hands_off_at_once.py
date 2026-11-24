"""Under OverflowAction.NEW, a full receiver that is still running hands its pool to a new receiver at once.

The full receiver stops taking contributions, then runs its later steps. The next contributor gets a new
receiver straight away instead of waiting for those steps, so the two receivers overlap. An explicit
SequentialStashStrategy keeps one receiver at a time.
"""
import asyncio
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.config import OrcaConfig
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.capacity import CapacityPolicy
from orca.resource_models.labware import CanContinueFn, LabwareInstance, PlateTemplate
from orca.state.records import DeclaredTracking
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.labware_group import LabwareGroup, LabwareGroupMember
from orca.workflow_models.labware_state import IOverflowStrategy, LabwareSlot
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import ActionTemplate, IMethodTemplate
from orca.workflow_models.overflow_strategy import SequentialStashStrategy
from orca.workflow_models.submission import BatchMode
from orca.workflow_models.thread_context import ThreadContext
from tests.closure_e2e_scaffold import TERMINAL_STATUSES
from tests.memory_activity import memory_labware_service, memory_unsaved_positions
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.test_helpers import create_test_device, create_test_transporter, wait_until, wire_system_map

_LABWARE_TYPE = "Cor_Falcon_96_wellplate_340ul_Fb_Black"


async def _runtime(
    transfers: list[str], released: asyncio.Event, overflow_strategy: IOverflowStrategy | None,
    used_up: bool = False, asked_after_the_shake: dict[str, bool] | None = None,
) -> tuple[SystemRuntime, WorkflowTemplate]:
    """One transfer station. Each receiver takes one contribution, then shakes once `released` is set.

    `used_up` leaves the receiver room for more but says its labware is used up after one transfer.
    """
    station = create_test_device("station", site_names=["site-1", "site-2"])
    shaker = create_test_device("shaker")
    transporter = create_test_transporter(
        "robot1", ["sample_pad", "station", "shaker", "final_pad", "end_1", "end_2", "waste"],
    )
    sample = PlateTemplate("sample", labware_type=_LABWARE_TYPE)
    final = PlateTemplate(
        "final", labware_type=_LABWARE_TYPE,
        group_sharing=GroupSharing.SHARED_ACROSS_GROUPS, submission_batching=SubmissionBatching.BATCHABLE,
        can_continue_fn=_not_after_one_transfer(transfers) if used_up else None,
    )
    registry = ResourceRegistry()
    for resource in (station, shaker, transporter):
        registry.add_resource(resource)
    station_pool, shaker_pool = ResourcePool("station", [station]), ResourcePool("shaker", [shaker])
    registry.add_resource_pool(station_pool)
    registry.add_resource_pool(shaker_pool)
    system_map = SystemMap(registry)
    await wire_system_map(
        system_map, devices={"station": station, "shaker": shaker},
        pads=["sample_pad", "final_pad", "end_1", "end_2", "waste"],
    )

    @orca.action(device=station_pool, inputs=[sample, final])
    async def transfer(ctx: ActionContext) -> None:
        transfers.append(ctx.labware("final").id)
        await ctx.device().shake(duration=1, speed=100)

    async def _transfer_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield transfer
    transfer_method = MethodTemplate("transfer_method", func=_transfer_method)

    @orca.action(device=shaker_pool, inputs=[final])
    async def shake_when_released(ctx: ActionContext) -> None:
        await released.wait()
        await ctx.device().shake(duration=1, speed=200)

    async def _shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_when_released
    shake_method = MethodTemplate("shake_method", func=_shake_method)

    async def _sample_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield transfer_method
    sample_thread = ThreadTemplate(
        labware_template=sample, start=system_map.get_location("sample_pad"),
        end=system_map.get_location("waste"), func=_sample_thread, contributes_to=["final"],
    )

    async def _final_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        while ctx.has_more_work():
            yield orca.join(allows=[transfer_method])
        yield shake_method
        if asked_after_the_shake is not None:
            asked_after_the_shake[ctx.labware.id] = ctx.has_more_work()
    final_thread = ThreadTemplate(
        labware_template=final, start=system_map.get_location("final_pad"),
        end=[system_map.get_location("end_1"), system_map.get_location("end_2")], func=_final_thread,
    )

    workflow = WorkflowTemplate("pooling")
    workflow.add_thread(sample_thread, is_start=True)
    workflow.add_thread(final_thread)
    workflow.register_auto_spawn(
        final_thread, capacity=CapacityPolicy(max_contributions=5 if used_up else 1),
        overflow_strategy=overflow_strategy,
    )
    builder = SdkToSystemBuilder(
        name="pooling", description="", labwares=[sample, final], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=EventBus(), config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    runtime = await memory_runtime(builder.get_system())
    await runtime.start()
    return runtime, workflow


def _not_after_one_transfer(transfers: list[str]) -> CanContinueFn:
    async def can_continue(instance: LabwareInstance, demand: DeclaredTracking | None) -> bool:
        return instance.id not in transfers
    return can_continue


def _final_slot(runtime: SystemRuntime, execution_id: str) -> LabwareSlot | None:
    workflow = runtime._executions[execution_id].executing_workflow
    if workflow is None or workflow._labware_registry is None:
        return None
    return next((s for s in workflow._labware_registry.all_slots().values() if s.labware_template_name == "final"), None)


async def _submit_two_samples(runtime: SystemRuntime, workflow: WorkflowTemplate) -> str:
    groups = [LabwareGroup(id=g, members=(LabwareGroupMember(thread_template_name="sample"),)) for g in ("g1", "g2")]
    submission = await runtime.submit(workflow, groups=groups, batch_mode=BatchMode.STANDALONE, mode=WorkflowRunMode.CHECK_RUN)
    return submission.execution_id


async def _all_complete(runtime: SystemRuntime, execution_id: str) -> None:
    def ended() -> bool:
        statuses = [t.status for t in runtime.list_threads(execution_id)]
        return bool(statuses) and all(s in TERMINAL_STATUSES for s in statuses)

    await wait_until(ended, timeout=60.0, message=f"the run never ended: {runtime.list_threads(execution_id)}")
    assert {t.status for t in runtime.list_threads(execution_id)} == {"COMPLETED"}


async def test_the_next_contributor_gets_a_new_receiver_while_the_full_one_still_runs() -> None:
    transfers: list[str] = []
    released = asyncio.Event()
    runtime, workflow = await _runtime(transfers, released, overflow_strategy=None)
    try:
        execution_id = await _submit_two_samples(runtime, workflow)

        await wait_until(lambda: len(transfers) == 2, timeout=30.0,
                         message="the second contribution waited for the full receiver to finish")
        assert len(set(transfers)) == 2

        released.set()
        await _all_complete(runtime, execution_id)
    finally:
        released.set()
        await runtime.shutdown()


async def test_a_used_up_receiver_still_running_hands_off_at_once() -> None:
    transfers: list[str] = []
    released = asyncio.Event()
    runtime, workflow = await _runtime(transfers, released, overflow_strategy=None, used_up=True)
    try:
        execution_id = await _submit_two_samples(runtime, workflow)

        await wait_until(lambda: len(transfers) == 2, timeout=30.0,
                         message="the second contribution waited for the used-up receiver to finish")
        assert len(set(transfers)) == 2

        released.set()
        await _all_complete(runtime, execution_id)
    finally:
        released.set()
        await runtime.shutdown()


async def test_a_receiver_that_handed_off_has_no_more_work() -> None:
    transfers: list[str] = []
    asked: dict[str, bool] = {}
    released = asyncio.Event()
    runtime, workflow = await _runtime(transfers, released, overflow_strategy=None, asked_after_the_shake=asked)
    try:
        execution_id = await _submit_two_samples(runtime, workflow)
        await wait_until(lambda: len(transfers) == 2, timeout=30.0, message="the second receiver never started")

        released.set()
        await _all_complete(runtime, execution_id)
        assert asked == {transfers[0]: False, transfers[1]: False}
    finally:
        released.set()
        await runtime.shutdown()


async def test_an_explicit_stash_waits_for_the_full_receiver_to_finish() -> None:
    transfers: list[str] = []
    released = asyncio.Event()
    runtime, workflow = await _runtime(transfers, released, overflow_strategy=SequentialStashStrategy())
    try:
        execution_id = await _submit_two_samples(runtime, workflow)

        def stashed() -> bool:
            slot = _final_slot(runtime, execution_id)
            return slot is not None and len(slot.pending) == 1

        await wait_until(stashed, timeout=30.0, message="the second contribution was never stashed")
        assert len(transfers) == 1

        released.set()
        await _all_complete(runtime, execution_id)
        assert len(set(transfers)) == 2
    finally:
        released.set()
        await runtime.shutdown()
