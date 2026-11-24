"""A thread that cannot spawn the thread bringing its action's other labware pauses, and AUTO_SPAWN_FAILED is recorded.

The incident names the labware it asked for and the thread that asked. Once the cause is fixed, RETRY spawns
again and the run finishes.
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.config import OrcaConfig
from orca.events.event_bus import EventBus
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.incident_store import IncidentCategory, RecoveryAction
from orca.runtime.system_runtime import ExecutionState
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.reservation_manager.errors import AutoSpawnFailedDetail
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.labware_threads.executing_labware_thread import AutoSpawnCallback
from orca.workflow_models.method import ExecutingMethod

from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.status_enums import RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflows.executing_workflow import ExecutingWorkflow
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service, memory_unsaved_positions
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.mock import UniversalMockDevice
from tests.test_helpers import create_test_plate_template, create_test_transporter, wait_until, wire_system_map


_REAL_CALLBACK = ExecutingWorkflow._make_auto_spawn_callback
_failures_left = [0]


def _a_spawn_that_fails_once(self: ExecutingWorkflow) -> AutoSpawnCallback:
    real = _REAL_CALLBACK(self)
    assert real is not None

    async def spawn(labware_name: str, method: ExecutingMethod, run_mode: WorkflowRunMode) -> None:
        if _failures_left[0] > 0:
            _failures_left[0] -= 1
            raise RuntimeError("the partner's start is not reachable")
        await real(labware_name, method, run_mode)

    return spawn


async def test_a_failed_auto_spawn_pauses_the_thread_and_retry_spawns_again(monkeypatch: pytest.MonkeyPatch) -> None:
    _failures_left[0] = 1
    monkeypatch.setattr(ExecutingWorkflow, "_make_auto_spawn_callback", _a_spawn_that_fails_once)
    combiner = UniversalMockDevice("combiner", site_names=["site-1", "site-2"])
    transporter = create_test_transporter("robot1", ["combiner", "pad1", "pad2"])
    first = create_test_plate_template("first")
    second = create_test_plate_template("second")
    registry = ResourceRegistry()
    registry.add_resource(combiner)
    registry.add_resource(transporter)
    pool = ResourcePool("combiner", [combiner])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"combiner": combiner}, pads=["pad1", "pad2"])

    @orca.action(device=pool, inputs=[first, second])
    async def combine(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=100)

    @orca.method
    async def combine_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield combine

    @orca.thread(labware=first, start=system_map.get_location("pad1"), end=system_map.get_location("pad1"))
    async def first_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield combine_method

    @orca.thread(labware=second, start=system_map.get_location("pad2"), end=system_map.get_location("pad2"))
    async def second_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join(allows=[combine_method])

    @orca.workflow(name="spawn_fails")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(first_journey)
        wf.thread(second_journey)

    builder = SdkToSystemBuilder(
        name="spawn", description="", labwares=[first, second], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=EventBus(), config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    runtime = await memory_runtime(builder.get_system())
    await runtime.start()
    try:
        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.CHECK_RUN)
        await wait_until(lambda: [t.status for t in runtime.list_threads(record.id)] == ["PAUSED"], timeout=10.0)
        [thread] = runtime.list_threads(record.id)
        assert thread.pause_site == "SPAWN_CAPACITY"

        [incident] = await runtime.incidents.list(category=IncidentCategory.AUTO_SPAWN_FAILED)
        assert incident.detail == AutoSpawnFailedDetail(requested_labware_name="second", requesting_thread_id=thread.id)
        assert incident.recovery_action is RecoveryAction.THREAD_RECOVER_RETRY
        assert "not reachable" in incident.message

        runtime.recover_thread(record.id, thread.id, RecoveryDecision.RETRY)
        final = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
        assert final.status is ExecutionState.COMPLETED
    finally:
        await runtime.shutdown()
