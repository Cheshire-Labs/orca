"""A pooled plate that shakes on either shaker, and plates pinned to one shaker each."""

from collections.abc import AsyncGenerator

import orca.orca as orca
from orca.devices.device_interfaces import IShaker
from orca.devices.shaker import Shaker
from orca.spawn import DISPENSE
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.sdk_features.kit import Lab, plate_template

POOLED, PINNED = 100.0, 200.0


def _pooled_thread(lab: Lab, wait_for: list[str]) -> ThreadTemplate:
    """The pooled plate waits for each named pinned plate to hold its shaker before it asks the pool."""
    pooled = plate_template("pooled")

    @orca.action(device=lab.shakers, inputs=[pooled])
    async def shake_on_any(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=int(POOLED))

    @orca.method
    async def pooled_method(ctx: MethodContext):
        yield shake_on_any

    @orca.thread(labware=pooled, start=("stacker", DISPENSE), end="waste")
    async def pooled_journey(ctx: ThreadContext):
        for event in wait_for:
            yield orca.on(event)
        yield pooled_method

    return pooled_journey


def _pinned_thread(shaker: Shaker, pad: str) -> ThreadTemplate:
    pinned = plate_template(f"on_{shaker.name}")

    @orca.action(device=shaker, inputs=[pinned])
    async def shake_here(ctx: ActionContext) -> None:
        await ctx.emit(holding(shaker.name))
        await ctx.device(IShaker).shake(duration=1, speed=int(PINNED))

    async def shake_it_here(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_here

    pinned_method = MethodTemplate(f"shake_on_{shaker.name}", func=shake_it_here)

    @orca.thread(labware=pinned, start=pad, end=pad)
    async def pinned_journey(ctx: ThreadContext):
        yield pinned_method

    return pinned_journey


def holding(shaker: str) -> str:
    return f"{shaker}_held_by_its_pinned_plate"


def pool_workflow(lab: Lab, *, pin_1: bool = False, pin_2: bool = False) -> WorkflowTemplate:
    """A pooled plate, plus a plate pinned to each shaker asked for; the pooled one starts after the pinned ones hold."""
    pinned = [name for name, wanted in (("shaker_1", pin_1), ("shaker_2", pin_2)) if wanted]
    entries = [_pooled_thread(lab, [holding(name) for name in pinned])]
    if pin_1:
        entries.append(_pinned_thread(lab.shaker_1, "pad_1"))
    if pin_2:
        entries.append(_pinned_thread(lab.shaker_2, "pad_2"))

    @orca.workflow(name="pool")
    def workflow(wf: WorkflowContext) -> None:
        for entry in entries:
            wf.start(entry)

    return workflow
