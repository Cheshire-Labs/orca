"""A one-plate workflow that reads a variable in its thread, method and two actions, held open between the actions."""

import asyncio
from dataclasses import dataclass, field

import orca.orca as orca
from orca.devices.device_interfaces import IShaker
from orca.variables import OptionValue, VariableDefinition
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.sdk_features.kit import Lab, plate_template


@dataclass
class Reads:
    """What each context's `ctx.param` returned, and a gate the first action waits on before reading."""

    variable: str
    before_first_read: asyncio.Event = field(default_factory=asyncio.Event)
    between_actions: asyncio.Event = field(default_factory=asyncio.Event)
    first_action_done: asyncio.Event = field(default_factory=asyncio.Event)
    seen: dict[str, list[OptionValue]] = field(default_factory=lambda: {"thread": [], "method": [], "first": [], "second": []})


def variable_workflow(
    lab: Lab, reads: Reads, definitions: dict[str, VariableDefinition], *, name: str = "reads_a_variable",
) -> WorkflowTemplate:
    """Each action shakes at the speed it read, so the value is checked at the device too."""
    plate = plate_template()

    @orca.action(device=lab.shaker_1, inputs=[plate])
    async def first(ctx: ActionContext) -> None:
        await reads.before_first_read.wait()
        speed = await ctx.param(reads.variable, int)
        reads.seen["first"].append(speed)
        await ctx.device(IShaker).shake(duration=1, speed=speed)
        reads.first_action_done.set()
        await reads.between_actions.wait()

    @orca.action(device=lab.shaker_1, inputs=[plate])
    async def second(ctx: ActionContext) -> None:
        speed = await ctx.param(reads.variable, int)
        reads.seen["second"].append(speed)
        await ctx.device(IShaker).shake(duration=1, speed=speed)

    @orca.method
    async def read_method(ctx: MethodContext):
        reads.seen["method"].append(await ctx.param(reads.variable))
        yield first
        yield second

    @orca.thread(labware=plate, start="pad_1", end="pad_1")
    async def plate_journey(ctx: ThreadContext):
        reads.seen["thread"].append(await ctx.param(reads.variable))
        yield read_method

    @orca.workflow(name=name)
    def workflow(wf: WorkflowContext) -> None:
        for variable, definition in definitions.items():
            wf.variable(variable, definition)
        wf.start(plate_journey)

    return workflow
