"""A stop that lands while an action runs still ends the thread when the action then fails.

The stop arrives first, so the thread is not yet paused and nobody sends it a
decision. The failing action then pauses it, and the pause must take the stop
it was already given instead of waiting for an operator who will never answer.
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.config import OrcaConfig
from orca.events.event_bus import EventBus
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.labware import LabwareInstance
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.state.records import ObservationGapCause
from orca.system.system_interface import ISystem
from orca.workflow_models.status_enums import RecoveryDecision
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext

from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.memory_activity import memory_unsaved_positions
from tests.mock import UniversalMockDevice
from tests.test_helpers import create_test_plate_template, create_test_transporter, wait_until, wire_system_map
from tests.memory_activity import memory_labware_service

pytestmark = pytest.mark.asyncio

_TERMINAL = frozenset({"STOPPED", "COMPLETED", "ABORTED", "FAILED"})


class _FailsWhenLetGo(UniversalMockDevice):
    """Holds its shake until the test lets go, then fails it."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.shaking = asyncio.Event()
        self.let_go = asyncio.Event()

    async def shake(self, duration: int, speed: int) -> None:
        self.shaking.set()
        await self.let_go.wait()
        raise RuntimeError("the shake failed after the stop")


async def _started() -> tuple[SystemRuntime, ISystem, _FailsWhenLetGo]:
    device = _FailsWhenLetGo("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate")
    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate])
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_action

    @orca.thread(labware=plate, start=system_map.get_location("pad1"), end=system_map.get_location("pad1"))
    async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield shake_method

    @orca.workflow(name="stop_then_fail_wf")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="stop_then_fail", description="", labwares=[plate], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=event_bus, config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(),
        unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    system = builder.get_system()
    runtime = await memory_runtime(system, event_bus=event_bus)
    await runtime.start()
    return runtime, system, device


async def _stopped_mid_action(runtime: SystemRuntime, system: ISystem, device: _FailsWhenLetGo) -> str:
    record = await runtime.submit_workflow("stop_then_fail_wf", mode=WorkflowRunMode.CHECK_RUN)
    await asyncio.wait_for(device.shaking.wait(), timeout=15.0)
    system.stop_all_threads()
    return record.id


async def _ends_as(runtime: SystemRuntime, execution_id: str) -> list[str]:
    await wait_until(lambda: all(t.status in _TERMINAL for t in runtime.list_threads(execution_id)), timeout=15.0)
    return [t.status for t in runtime.list_threads(execution_id)]


async def test_a_stop_during_an_action_that_then_fails_aborts_the_thread() -> None:
    runtime, system, device = await _started()
    try:
        execution_id = await _stopped_mid_action(runtime, system, device)
        device.let_go.set()

        assert await _ends_as(runtime, execution_id) == ["ABORTED"]
    finally:
        device.let_go.set()
        await runtime.shutdown()


async def test_an_operator_decision_sent_before_the_pause_settles_does_not_outlive_the_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decision lands while the pause is still entering, before anything watches the stop."""
    entering = asyncio.Event()
    decided = asyncio.Event()
    unpatched = LabwareInstance.note_observation_gap

    async def held_until_decided(self: LabwareInstance, cause: ObservationGapCause) -> None:
        if cause is ObservationGapCause.ERROR_PAUSE:
            entering.set()
            await decided.wait()
        await unpatched(self, cause)

    monkeypatch.setattr(LabwareInstance, "note_observation_gap", held_until_decided)
    runtime, system, device = await _started()
    try:
        execution_id = await _stopped_mid_action(runtime, system, device)
        device.let_go.set()
        await asyncio.wait_for(entering.wait(), timeout=15.0)
        [thread] = runtime.list_threads(execution_id)
        runtime.recover_thread(execution_id, thread.id, RecoveryDecision.RETRY)
        decided.set()

        assert await _ends_as(runtime, execution_id) == ["ABORTED"]
    finally:
        decided.set()
        device.let_go.set()
        await runtime.shutdown()
