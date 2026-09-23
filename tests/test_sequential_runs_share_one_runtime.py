"""Two runs back to back on one runtime, the way the bench actually drives it.

Earlier tests built a fresh runtime per workflow, so nothing covered the
sequence an operator runs: submit, wait, submit the next. A thread that ends
LEAVE_IN_PLACE leaves its labware in a device slot on purpose, and the question
this pins is whether it also leaves the DEVICE reserved. If it does, the next
run waits on that device forever and the deployment looks hung.
"""

from typing import AsyncGenerator, Tuple

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.spawn import LEAVE_IN_PLACE
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext

from tests.test_helpers import (
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
    execution_outcome,
    wire_system_map,
)


async def _system_with_a_resident_and_a_visitor() -> Tuple[
    ISystem, WorkflowTemplate, WorkflowTemplate
]:
    """A two-site shaker. `leave_resident` parks a plate on one site and ends
    there; `visit_shaker` is an ordinary run that needs the same shaker after,
    on the other site. Two sites because a resident is entitled to hold the SITE
    it sits on; what it must not do is hold the whole device."""
    device = create_test_device("shaker1", site_names=["slot_a", "slot_b"])
    transporter = create_test_transporter("robot1", ["shaker1", "pad1", "pad2"])
    resident_plate = create_test_plate_template("resident_plate")
    visiting_plate = create_test_plate_template("visiting_plate")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map, devices={"shaker1": device}, pads=["pad1", "pad2"],
    )

    @orca.action(device=pool, inputs=[resident_plate])
    async def settle(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def settle_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield settle

    @orca.action(device=pool, inputs=[visiting_plate])
    async def visit(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def visit_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield visit

    shaker_slot = system_map.get_location("shaker1/slot_a")

    @orca.thread(
        labware=resident_plate,
        start=shaker_slot,
        end=(shaker_slot, LEAVE_IN_PLACE),
    )
    async def resident(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield settle_method

    pad2 = system_map.get_location("pad2")

    @orca.thread(labware=visiting_plate, start=pad2, end=pad2)
    async def visitor(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield visit_method

    leave_resident = WorkflowTemplate("leave_resident")
    leave_resident.add_thread(resident, is_start=True)
    visit_shaker = WorkflowTemplate("visit_shaker")
    visit_shaker.add_thread(visitor, is_start=True)

    builder = SdkToSystemBuilder(
        name="test_system",
        description="",
        labwares=[resident_plate, visiting_plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[leave_resident, visit_shaker],
        event_bus=EventBus(),
    )
    await builder.bind_labwares()
    return builder.get_system(), leave_resident, visit_shaker


async def test_a_finished_run_frees_the_device_its_resident_sits_in() -> None:
    """A resident holds the SITE it sits on for good; it must not hold the DEVICE.

    The device mutex releases on a drain predicate, and labware declared resident
    is meant to be excluded from it. A thread that ends LEAVE_IN_PLACE was not,
    so the release waited on a departure the workflow had declared would never
    happen and the reservation outlived the run. The next execution needing that
    device then waits forever, with nothing logged to say what it is waiting on.
    """
    system, leave_resident, _visit_shaker = await _system_with_a_resident_and_a_visitor()
    runtime = SystemRuntime(system)
    await runtime.start()
    try:
        first = await runtime.submit(leave_resident, mode=WorkflowRunMode.PURE_SIM)
        assert (await execution_outcome(runtime, first, timeout=60.0)).status == "completed"
        held = runtime.list_reservations(first.execution_id)
    finally:
        await runtime.shutdown()

    devices_held = [r.position_id for r in held if "/" not in r.position_id]
    assert devices_held == [], (
        "a completed run still holds %s" % (devices_held,)
    )
