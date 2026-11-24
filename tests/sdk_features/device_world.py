"""Small one-plate workflows the device and start-kind tests share."""

import orca.orca as orca
from orca.devices.device_interfaces import ILiquidHandler, IShaker
from orca.spawn import DISPENSE
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import EndArg, StartArg
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.sdk_features.kit import (
    PLATE_SITE,
    TIP_SITE,
    Lab,
    plate_template,
    tips_template,
    tips_thread,
)


def shake_once(lab: Lab, start: StartArg, end: EndArg, *, name: str = "one_plate") -> WorkflowTemplate:
    plate = plate_template()

    @orca.action(device=lab.shaker_1, inputs=[plate])
    async def shake(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=100)

    @orca.method
    async def shake_method(ctx: MethodContext):
        yield shake

    @orca.thread(labware=plate, start=start, end=end)
    async def plate_journey(ctx: ThreadContext):
        yield shake_method

    @orca.workflow(name=name)
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)

    return workflow


def shake_then_take(lab: Lab) -> WorkflowTemplate:
    """A plate from the stacker shakes, then gives 5 uL from A1 using one tip from the deck-resident rack."""
    plate = plate_template()
    tips = tips_template()

    @orca.action(device=lab.shaker_1, inputs=[plate])
    async def shake(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=100)

    @orca.action(device=lab.lh, inputs=[plate, tips], deck_positions={plate: PLATE_SITE, tips: TIP_SITE})
    async def take(ctx: ActionContext) -> None:
        lh = ctx.device(ILiquidHandler)
        await lh.pick_up_tips(await ctx.next_tips("tips", 1))
        await lh.aspirate([ctx.plate("plate").well("A1")], [5.0])
        await lh.discard_tips()

    @orca.method
    async def shake_method(ctx: MethodContext):
        yield shake

    @orca.method
    async def take_method(ctx: MethodContext):
        yield take

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield shake_method
        yield take_method

    tips_journey = tips_thread(tips, [take_method])

    @orca.workflow(name="shake_then_take")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)
        wf.thread(tips_journey)

    return workflow
