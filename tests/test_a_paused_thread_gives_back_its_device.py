"""A thread that parks to wait on a person gives back the device it holds.

The wait ends when an operator decides, which can be hours. Holding a device
mutex through it locks out every sibling that needs the device, for no work.

The window this happens in is real and narrow: after a method's last action the
thread keeps that device, because the action-completion handler sets the method
completed before the loop can ask for another action and take the release
branch. Anything that parks the thread before it resolves its next action parks
it holding a device it is not using. An action-resolution failure does exactly
that.
"""
from typing import AsyncGenerator

import pytest

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from tests.mock import UniversalMockDevice
from tests.mutation_helpers import wait_for_paused
from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    wire_system_map,
)


async def _paused_after_a_method_finished_on_a_device() -> tuple[SystemRuntime, str]:
    """A thread that finished a method on ``shaker_1`` and then failed to
    resolve its next one, so it is parked waiting for an operator."""
    device = UniversalMockDevice("shaker_1")
    transporter = create_test_transporter("robot1", ["shaker_1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker_1", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker_1": device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate])
    async def shake(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def first(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake

    @orca.method
    async def second(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        raise RuntimeError("this method cannot say what to run")
        yield shake  # pragma: no cover -- makes this an async generator

    pad = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad, end=pad)
    async def journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield first
        yield second

    workflow = WorkflowTemplate("pause_holds_device")
    workflow.add_thread(journey, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[plate],
        resources_registry=registry, system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    runtime = SystemRuntime(builder.get_system(), event_bus=event_bus)
    await runtime.start()
    submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
    await wait_for_paused(runtime, submission.execution_id)
    return runtime, submission.execution_id


@pytest.mark.asyncio
async def test_a_thread_parked_on_an_operator_holds_no_device() -> None:
    runtime, execution_id = await _paused_after_a_method_finished_on_a_device()
    try:
        held = [r.position_id for r in runtime.list_reservations(execution_id)]
        assert "shaker_1" not in held, (
            f"the device is reserved while the thread waits on a person: {held}"
        )
    finally:
        await runtime.shutdown(confirm=True)
