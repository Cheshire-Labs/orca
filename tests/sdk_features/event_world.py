"""A plate thread that waits on an event between steps, and an emitter thread the test releases."""

import asyncio
import time
from collections.abc import AsyncGenerator, Callable

import orca.orca as orca
from orca.devices.device_interfaces import IShaker
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate, MethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.sdk_features.kit import Lab, plate_template


def event_workflow(
    lab: Lab,
    release: asyncio.Event,
    value: str,
    step: Callable[[dict[str, MethodTemplate]], IMethodTemplate],
    *,
    name: str,
    emitted_at: list[float] | None = None,
) -> WorkflowTemplate:
    """The waiter shakes on shaker_1 at 100, runs `step`, then shakes at 500.

    `step` may use the methods named a, b and else, which shake at 200, 300 and 400.
    """
    waiter, emitter = plate_template("waiter"), plate_template("emitter")

    def shaker_1_at(speed: int) -> MethodTemplate:
        @orca.action(device=lab.shaker_1, inputs=[waiter])
        async def shake(ctx: ActionContext) -> None:
            await ctx.device(IShaker).shake(duration=1, speed=speed)

        async def shake_at_speed(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shake

        return MethodTemplate(f"shake_at_{speed}", func=shake_at_speed)

    @orca.action(device=lab.shaker_2, inputs=[emitter])
    async def emit(ctx: ActionContext) -> None:
        await release.wait()
        if emitted_at is not None:
            emitted_at.append(time.perf_counter())
        await ctx.emit("go", value=value)

    @orca.method
    async def emit_method(ctx: MethodContext):
        yield emit

    first = shaker_1_at(100)
    choices = {"a": shaker_1_at(200), "b": shaker_1_at(300), "else": shaker_1_at(400)}
    waiting_step = step(choices)
    last = shaker_1_at(500)

    @orca.thread(labware=waiter, start="pad_1", end="pad_1")
    async def waiter_journey(ctx: ThreadContext):
        yield first
        yield waiting_step
        yield last

    @orca.thread(labware=emitter, start="pad_2", end="pad_2")
    async def emitter_journey(ctx: ThreadContext):
        yield emit_method

    @orca.workflow(name=name)
    def workflow(wf: WorkflowContext) -> None:
        wf.start(waiter_journey)
        wf.start(emitter_journey)

    return workflow
