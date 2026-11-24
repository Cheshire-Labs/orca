"""`thread(required=False)` spawns only for a group that names it; a group that leaves it out runs without it."""

import asyncio
from collections.abc import AsyncGenerator
from uuid import uuid4

import orca.orca as orca
from orca.config import OrcaConfig
from orca.events.event_bus import EventBus
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.labware_group import LabwareGroup, LabwareGroupMember
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service, memory_unsaved_positions
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.mock import UniversalMockDevice
from tests.test_helpers import create_test_plate_template, create_test_transporter, wire_system_map


class _CountsShakes(UniversalMockDevice):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.speeds: list[int] = []

    async def shake(self, duration: int, speed: int) -> None:
        self.speeds.append(speed)
        await super().shake(duration, speed)


async def _a_sample_and_an_optional_control() -> tuple[SystemRuntime, WorkflowTemplate, _CountsShakes]:
    shaker = _CountsShakes("shaker")
    transporter = create_test_transporter("robot1", ["shaker", "pad1", "pad2"])
    sample = create_test_plate_template("sample")
    control = create_test_plate_template("control")
    registry = ResourceRegistry()
    registry.add_resource(shaker)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker", [shaker])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker": shaker}, pads=["pad1", "pad2"])

    @orca.action(device=pool, inputs=[sample])
    async def shake_sample(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=100)

    @orca.action(device=pool, inputs=[control])
    async def shake_control(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=200)

    @orca.method
    async def sample_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_sample

    @orca.method
    async def control_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_control

    pad1 = system_map.get_location("pad1")
    pad2 = system_map.get_location("pad2")

    @orca.thread(labware=sample, start=pad1, end=pad1)
    async def sample_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield sample_method

    @orca.thread(labware=control, start=pad2, end=pad2, required=False)
    async def control_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield control_method

    @orca.workflow(name="optional_control")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(sample_journey)
        wf.start(control_journey)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="optional", description="", labwares=[sample, control], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=event_bus, config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    return await memory_runtime(builder.get_system(), event_bus=event_bus), workflow, shaker


async def _run(*members: str) -> tuple[list[str], ExecutionState, list[int]]:
    runtime, workflow, shaker = await _a_sample_and_an_optional_control()
    await runtime.start()
    try:
        groups = [LabwareGroup(id=str(uuid4()), members=tuple(LabwareGroupMember(m) for m in members))]
        submission = await runtime.submit(workflow, groups=groups, mode=WorkflowRunMode.CHECK_RUN)
        final = await asyncio.wait_for(runtime.wait(submission.execution_id), timeout=10.0)
        threads = sorted(t.name.split("-")[0] for t in runtime.list_threads(submission.execution_id))
        return threads, final.status, sorted(shaker.speeds)
    finally:
        await runtime.shutdown()


async def test_a_group_that_leaves_the_optional_thread_out_runs_without_it() -> None:
    threads, status, speeds = await _run("sample_journey")

    assert threads == ["sample"], "the optional control never spawned"
    assert status is ExecutionState.COMPLETED
    assert speeds == [100]


async def test_a_group_that_names_the_optional_thread_runs_it() -> None:
    threads, status, speeds = await _run("sample_journey", "control_journey")

    assert threads == ["control", "sample"]
    assert status is ExecutionState.COMPLETED
    assert speeds == [100, 200]


async def test_a_group_that_names_the_optional_thread_by_its_labware_name_runs_it() -> None:
    threads, status, speeds = await _run("sample", "control")

    assert threads == ["control", "sample"]
    assert status is ExecutionState.COMPLETED
    assert speeds == [100, 200]
