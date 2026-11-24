"""A workflow with two `wf.on` handlers and one method, to run that method on its own through `submit_method`."""
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

from cheshire_drivers import CartesianCoordinates as C
from cheshire_drivers import Teachpoint

import orca.orca as orca
from orca.devices.shaker import Shaker
from orca.events.event_bus import EventBus
from orca.events.execution_context import ExecutionContext
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.transporter import Transporter
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import SystemBuild, Topology
from orca.sdk.labware import PlateTemplate
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import ActionTemplate, MethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.setup_package import package_stores, with_teachpoints, write_teachpoints

READ_DONE = "CUSTOM.read_done.EMITTED"
ACTION_COMPLETED = "ACTION.COMPLETED"
METHOD = {"workflow_name": "reading", "method_name": "read_method",
          "labware_start": {"plate": "pad_1"}, "labware_end": {"plate": "pad_1"}}


@dataclass
class Heard:
    """The events each of the workflow's two handlers was called for."""

    read_done: list[str] = field(default_factory=list)
    action_completed: list[str] = field(default_factory=list)


async def a_built_reading_workflow(heard: Heard) -> SystemBuild:
    stores = package_stores()
    shaker = Shaker("reader")
    pool = ResourcePool("readers", [shaker])
    arm = with_teachpoints(Transporter("arm"), write_teachpoints(stores, "arm", [
        Teachpoint(position, C(100.0 * i, 200, 300, 0, 90, 180), orientation="right")
        for i, position in enumerate(["pad_1", "reader"])
    ]))
    topology = Topology(locations={"reader": shaker, "pad_1": PlatePad("pad_1")}, transporters=[arm], pools=[pool])
    plate = PlateTemplate("plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")

    @orca.action(device=pool, inputs=[plate])
    async def read(ctx: ActionContext) -> None:
        await ctx.emit("read_done", "ok")

    @orca.method
    async def read_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield read

    @orca.thread(labware=plate, start="pad_1", end="pad_1")
    async def plate_journey(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield read_method

    def on_read_done(event_name: str, context: ExecutionContext) -> None:
        heard.read_done.append(event_name)

    def on_action_completed(event_name: str, context: ExecutionContext) -> None:
        heard.action_completed.append(event_name)

    @orca.workflow(name="reading")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)
        wf.on(READ_DONE, on_read_done)
        wf.on(ACTION_COMPLETED, on_action_completed)

    return await orca.build_system(
        name="reading", workflow=workflow, topology=topology, stores=stores,
        event_bus=EventBus(), ops_history_store=memory_ops_store(),
    )


async def a_started_runtime(system: ISystem, build: SystemBuild) -> SystemRuntime:
    runtime = await memory_runtime(system, event_bus=build.event_bus)
    await runtime.start()
    return runtime
