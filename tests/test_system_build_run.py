"""`SystemBuild.run` runs the bundled workflow once and raises unless it completes."""

import pytest
from cheshire_drivers import CartesianCoordinates, Teachpoint

import orca.orca as orca
from orca.devices.devices import Storage, Waste
from orca.devices.shaker import Shaker
from orca.resource_models.transporter import Transporter
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.sdk.build import SystemBuild, Topology
from orca.sdk.labware import PlateTemplate
from orca.spawn import DISPENSE
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.status_enums import FailurePolicy
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext

_PLATE = PlateTemplate("plate", "Cor_Falcon_96_wellplate_340ul_Fb_Black")


async def _build(shake_fails: bool) -> SystemBuild:
    stores = InMemoryRuntimeStoreFactory()
    shaker = Shaker("shaker")
    points = [
        Teachpoint(name, CartesianCoordinates(x, 0, 0, 0, 90, 180), orientation="right")
        for x, name in enumerate(["stacker", "shaker", "waste"])
    ]
    topology = Topology(
        locations={"stacker": Storage("stacker"), "shaker": shaker, "waste": Waste("waste")},
        transporters=[Transporter("arm", teachpoint_store=stores.teachpoints("arm", seed=points))],
    )

    @orca.action(device=shaker, inputs=[_PLATE], failure_policy=FailurePolicy.ABORT)
    async def shake(ctx: ActionContext) -> None:
        if shake_fails:
            raise RuntimeError("the shaker jammed")
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_step(ctx: MethodContext):
        yield shake

    @orca.thread(labware=_PLATE, start=("stacker", DISPENSE), end="waste")
    async def plate_thread(ctx: ThreadContext):
        yield shake_step

    @orca.workflow(name="shake_once")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_thread)

    return await orca.build_system(
        name="shake_once", workflow=workflow, topology=topology, stores=stores,
        configure_logging=False,
    )


@pytest.mark.timeout(120)
async def test_a_run_that_completes_returns() -> None:
    await (await _build(shake_fails=False)).run()


@pytest.mark.timeout(120)
async def test_a_run_that_fails_raises_with_the_reason() -> None:
    build = await _build(shake_fails=True)

    with pytest.raises(RuntimeError, match="the shaker jammed"):
        await build.run()
