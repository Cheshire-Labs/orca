"""What a running workflow body actually reads when a submission overrides a variable.

The existing submission-variable coverage stops at the submission record and the
variable store. Neither says whether the value reaches `ctx.param` inside a thread,
a method, or an action body, which is the only place authored code can read it.
It does not: the value lands in the store's per-submission partition, and the
method and action contexts resolve without a submission id, so every read falls
through to the workflow default. A run branching on `ctx.param` silently takes the
default branch.
"""

from typing import AsyncGenerator, Dict, List, Tuple

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_template import IMethodTemplate
from orca.variables import OptionValue, VariableDefinition

from tests.test_helpers import (
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
    execution_outcome,
    wire_system_map,
)

_DEFAULT_SHAKE_SECONDS = 60
_SUBMITTED_SHAKE_SECONDS = 120

ReadLog = Dict[str, List[OptionValue]]


async def _system_reading_shake_time(seen: ReadLog) -> Tuple[ISystem, WorkflowTemplate]:
    """A one-shake workflow whose thread, method and action each record what
    `ctx.param("shake_time")` gave them."""
    device = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate])
    async def shake_action(ctx: ActionContext) -> None:
        seen["action"].append(await ctx.param("shake_time"))
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        seen["method"].append(await ctx.param("shake_time"))
        yield shake_action

    pad = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad, end=pad)
    async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        seen["thread"].append(await ctx.param("shake_time"))
        yield shake_method

    workflow = WorkflowTemplate("shake_once")
    workflow.add_thread(plate_thread, is_start=True)

    builder = SdkToSystemBuilder(
        name="test_system",
        description="",
        labwares=[plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=EventBus(),
    )
    await builder.bind_labwares()
    system = builder.get_system()
    system.variable_store.register_workflow_definitions(
        workflow.name,
        {"shake_time": VariableDefinition(type="int", default=_DEFAULT_SHAKE_SECONDS)},
    )
    return system, workflow


async def test_every_context_reads_the_submitted_value_not_the_default() -> None:
    """A submission override has to be visible everywhere authored code can read
    it. Any context still resolving without the submission id hands back the
    workflow default, which is a wrong number that looks like a right one."""
    seen: ReadLog = {"thread": [], "method": [], "action": []}
    system, workflow = await _system_reading_shake_time(seen)
    runtime = SystemRuntime(system)
    await runtime.start()
    try:
        submission = await runtime.submit(
            workflow,
            variables={"shake_time": _SUBMITTED_SHAKE_SECONDS},
            mode=WorkflowRunMode.PURE_SIM,
        )
        status = await execution_outcome(runtime, submission, timeout=30.0)
    finally:
        await runtime.shutdown()

    assert status.status == "completed"
    for layer in ("thread", "method", "action"):
        assert seen[layer], f"{layer} context never read the variable"
        assert seen[layer] == [_SUBMITTED_SHAKE_SECONDS] * len(seen[layer]), (
            f"{layer} context resolved {seen[layer]}, so it never saw the submission"
        )


async def test_a_submission_that_overrides_nothing_still_reads_the_default() -> None:
    """The submission partition is a layer above the default, not a replacement:
    a name it does not carry has to fall through rather than go missing."""
    seen: ReadLog = {"thread": [], "method": [], "action": []}
    system, workflow = await _system_reading_shake_time(seen)
    runtime = SystemRuntime(system)
    await runtime.start()
    try:
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=30.0)
    finally:
        await runtime.shutdown()

    assert status.status == "completed"
    for layer in ("thread", "method", "action"):
        assert seen[layer] == [_DEFAULT_SHAKE_SECONDS] * len(seen[layer])
