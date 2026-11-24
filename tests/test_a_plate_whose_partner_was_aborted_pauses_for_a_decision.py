"""When the plate an action waits for is aborted, the waiting plate pauses for an operator decision.

Plate `first`'s combine needs plate `second`. `second` joins only `other_method`, so its join is refused and it
pauses; the operator aborts it. `first` must not sit PAUSED with nothing on offer: it pauses for error recovery,
names the aborted plate and the action, and offers ABORT_ACTION (skip the action) and ABORT_THREAD. RETRY is
refused: the action stays bound to the plate that will not come, so waiting again could never end. A plate whose
bringing thread is stopped instead pauses the same way.
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.config import OrcaConfig
from orca.events.event_bus import EventBus
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.status_models import ThreadSnapshot
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.status_enums import RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service, memory_unsaved_positions
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.mock import UniversalMockDevice
from tests.test_helpers import create_test_plate_template, create_test_transporter, wait_for_runtime_condition, wire_system_map


class _CountsShakes(UniversalMockDevice):
    def __init__(self, name: str, site_names: list[str] | None = None) -> None:
        super().__init__(name, site_names=site_names)
        self.shakes = 0

    async def shake(self, duration: int, speed: int) -> None:
        self.shakes += 1
        await super().shake(duration, speed)


async def _a_refused_joiner() -> tuple[SystemRuntime, str, _CountsShakes]:
    combiner = _CountsShakes("combiner", site_names=["site-1", "site-2"])
    shaker = UniversalMockDevice("shaker")
    transporter = create_test_transporter("robot1", ["combiner", "shaker", "pad1", "pad2", "pad3"])
    first = create_test_plate_template("first")
    second = create_test_plate_template("second")
    registry = ResourceRegistry()
    for resource in (combiner, shaker, transporter):
        registry.add_resource(resource)
    combiner_pool = ResourcePool("combiner", [combiner])
    shaker_pool = ResourcePool("shaker", [shaker])
    registry.add_resource_pool(combiner_pool)
    registry.add_resource_pool(shaker_pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"combiner": combiner, "shaker": shaker}, pads=["pad1", "pad2", "pad3"])

    @orca.action(device=combiner_pool, inputs=[first, second])
    async def combine(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=100)

    @orca.action(device=shaker_pool, inputs=[second])
    async def unrelated(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=200)

    @orca.method
    async def combine_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield combine

    @orca.method
    async def other_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield unrelated

    pad1 = system_map.get_location("pad1")
    pad2 = system_map.get_location("pad2")

    @orca.thread(labware=first, start=pad1, end=pad1)
    async def first_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield combine_method

    @orca.thread(labware=second, start=pad2, end=pad2)
    async def second_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join(allows=[other_method])

    @orca.workflow(name="join_not_allowed")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(first_journey)
        wf.thread(second_journey)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="join", description="", labwares=[first, second], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=event_bus, config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    runtime = await memory_runtime(builder.get_system(), event_bus=event_bus)
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.CHECK_RUN)
    return runtime, record.id, combiner


def _thread(runtime: SystemRuntime, execution_id: str, labware: str) -> ThreadSnapshot:
    [found] = [t for t in runtime.get_execution_detail(execution_id).threads if t.name.startswith(labware)]
    return found


async def _until_first_pauses_after_second_is_aborted(runtime: SystemRuntime, execution_id: str) -> ThreadSnapshot:
    def error_paused(labware: str) -> bool:
        threads = [t for t in runtime.get_execution_detail(execution_id).threads if t.name.startswith(labware)]
        return bool(threads) and threads[0].pause_reason == "error"

    await wait_for_runtime_condition(runtime, lambda: error_paused("second"), timeout=10.0, message="the refused join")
    second = _thread(runtime, execution_id, "second")
    runtime.recover_thread(execution_id, second.id, RecoveryDecision.ABORT_THREAD)
    await wait_for_runtime_condition(
        runtime, lambda: error_paused("first"), timeout=10.0, message="the waiting plate to pause for a decision",
    )
    return _thread(runtime, execution_id, "first")


async def test_the_waiting_plate_pauses_naming_the_aborted_plate_and_its_action() -> None:
    runtime, execution_id, _ = await _a_refused_joiner()
    try:
        first = await _until_first_pauses_after_second_is_aborted(runtime, execution_id)

        assert first.honoured_decisions == ("ABORT_ACTION", "ABORT_THREAD"), first
        assert "second" in (first.last_error or "") and "combine" in (first.last_error or ""), first.last_error
    finally:
        await runtime.shutdown()


async def test_abort_action_skips_the_combine_and_the_run_ends() -> None:
    runtime, execution_id, combiner = await _a_refused_joiner()
    try:
        first = await _until_first_pauses_after_second_is_aborted(runtime, execution_id)
        runtime.recover_thread(execution_id, first.id, RecoveryDecision.ABORT_ACTION)
        final = await asyncio.wait_for(runtime.wait(execution_id), timeout=10.0)

        assert combiner.shakes == 0
        assert _thread(runtime, execution_id, "first").status == "COMPLETED"
        assert final.status is ExecutionState.PARTIAL_COMPLETED
    finally:
        await runtime.shutdown()


async def test_abort_thread_ends_the_waiting_plate_and_the_run() -> None:
    runtime, execution_id, _ = await _a_refused_joiner()
    try:
        first = await _until_first_pauses_after_second_is_aborted(runtime, execution_id)
        runtime.recover_thread(execution_id, first.id, RecoveryDecision.ABORT_THREAD)
        final = await asyncio.wait_for(runtime.wait(execution_id), timeout=10.0)

        assert final.status is ExecutionState.ABORTED
    finally:
        await runtime.shutdown()


async def test_retry_is_refused() -> None:
    runtime, execution_id, _ = await _a_refused_joiner()
    try:
        first = await _until_first_pauses_after_second_is_aborted(runtime, execution_id)

        with pytest.raises(ValueError, match="does not honour"):
            runtime.recover_thread(execution_id, first.id, RecoveryDecision.RETRY)
    finally:
        await runtime.shutdown()


async def test_a_bringing_thread_stopped_while_paused_pauses_the_waiting_plate_too() -> None:
    runtime, execution_id, _ = await _a_refused_joiner()
    try:
        def error_paused(labware: str) -> bool:
            threads = [t for t in runtime.get_execution_detail(execution_id).threads if t.name.startswith(labware)]
            return bool(threads) and threads[0].pause_reason == "error"

        await wait_for_runtime_condition(runtime, lambda: error_paused("second"), timeout=10.0, message="the refused join")
        workflow = runtime._executions[execution_id].executing_workflow
        assert workflow is not None
        [second] = [t for t in workflow.threads if t.name.startswith("second")]
        second.stop()
        await wait_for_runtime_condition(
            runtime, lambda: error_paused("first"), timeout=10.0, message="the waiting plate to pause for a decision",
        )

        assert _thread(runtime, execution_id, "first").pause_site == "CO_LABWARE_LOST"
    finally:
        await runtime.shutdown()
