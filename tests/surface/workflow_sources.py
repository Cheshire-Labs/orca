"""Source files the suite adds to the package it mounts."""

MANUAL_STEP_WORKFLOW = '''
import orca.orca as orca
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from orca.sdk.workflow import (
    ActionContext, DISPENSE, MethodContext, ThreadContext, WorkflowContext, WorkflowTemplate,
)

check_plate = PlateTemplate("check_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")


def build_workflow(topology: Topology) -> WorkflowTemplate:
    shaker_pool = topology.pool("shaker_pool")

    @orca.action(device=shaker_pool, inputs=[check_plate])
    async def look_at_plate(ctx: ActionContext):
        await ctx.manual_step("Look at the plate")

    @orca.method
    async def check_step(ctx: MethodContext):
        yield look_at_plate

    @orca.thread(labware=check_plate, start=("stacker", DISPENSE), end="waste")
    async def check_journey(ctx: ThreadContext):
        yield check_step

    @orca.workflow(name="surface_manual")
    def surface_manual(wf: WorkflowContext):
        wf.start(check_journey)

    return surface_manual
'''

SHAKE_ACTION = """
from orca.devices.device_interfaces import IShaker
from orca.sdk.labware import AnyLabwareTemplate

@orca.action(device=topology.pool("shaker_pool"), inputs=[AnyLabwareTemplate()])
async def ad_hoc(ctx):
    await ctx.device(IShaker).shake(duration=1, speed=100)
"""
