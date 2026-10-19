"""`add_workflow_template` registers thread templates in the order `build_system(workflow=...)` does.

Both paths register the same templates. `build_system` lists them in
`wf.start` / `wf.thread` order, so a workflow added after the build must too,
or `list_thread_templates()` orders the same workflow two ways.
"""

from collections.abc import AsyncGenerator

import orca.orca as orca
from cheshire_drivers import AccessConfig, CartesianCoordinates, Teachpoint

from orca.devices.shaker import Shaker
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory, IRuntimeStoreFactory
from orca.sdk.build import Topology, add_workflow_template, build_system
from orca.sdk.labware import PlateTemplate
from orca.sdk.workflow import MethodTemplate
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate


def _topology(stores: IRuntimeStoreFactory) -> Topology:
    access = AccessConfig(name="vertical", access_type="vertical")
    stores.access_configs(seed=[access])
    points = [
        Teachpoint(name, CartesianCoordinates(x, 0, 0, 0, 90, 180), orientation="right", access=access)
        for x, name in ((0, "shaker_1"), (100, "pad_1"))
    ]
    arm = Transporter("arm", teachpoint_store=stores.teachpoints("arm", seed=points))
    return Topology(
        locations={"shaker_1": Shaker("shaker_1"), "pad_1": PlatePad("pad_1")},
        transporters=[arm],
    )


def _workflow_decorated_in_the_other_order(topology: Topology) -> WorkflowTemplate:
    """Threads decorate as first_plate then second_plate; the workflow starts second_plate."""
    first_plate = PlateTemplate("first_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    second_plate = PlateTemplate("second_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    shaker = topology.device("shaker_1", Shaker)

    @orca.action(device=shaker, inputs=[first_plate])
    async def shake_first(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=100)

    @orca.action(device=shaker, inputs=[second_plate])
    async def shake_second(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=100)

    @orca.method
    async def shake_first_step(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_first

    @orca.method
    async def shake_second_step(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_second

    @orca.thread(labware=first_plate, start="pad_1", end="pad_1")
    async def first_journey(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield shake_first_step

    @orca.thread(labware=second_plate, start="pad_1", end="pad_1")
    async def second_journey(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield shake_second_step

    @orca.workflow(name="two_plates")
    def two_plates(wf: WorkflowContext) -> None:
        wf.start(second_journey)
        wf.thread(first_journey)

    return two_plates


async def test_a_workflow_added_after_the_build_registers_threads_in_the_build_order() -> None:
    built_stores = InMemoryRuntimeStoreFactory()
    built_topology = _topology(built_stores)
    built = await build_system(
        "built_with_workflow", built_topology, built_stores,
        workflow=_workflow_decorated_in_the_other_order(built_topology),
        configure_logging=False,
    )

    added_stores = InMemoryRuntimeStoreFactory()
    added_topology = _topology(added_stores)
    added = await build_system(
        "workflow_added_later", added_topology, added_stores, configure_logging=False,
    )
    await add_workflow_template(added.system, _workflow_decorated_in_the_other_order(added_topology))

    built_order = list(built.system.get_labware_thread_templates())
    assert built_order == [("two_plates", "second_plate"), ("two_plates", "first_plate")]
    assert list(added.system.get_labware_thread_templates()) == built_order
