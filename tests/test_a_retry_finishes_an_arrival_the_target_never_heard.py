"""A place whose arrival notification failed is finished by the retry.

A move ends by telling the target it has the plate, and that call reaches a
real device, so it can fail on its own. The plate is on the target by then --
the arm let go before it -- so the retry has nothing to carry, only something
to say.

It said nothing. The plate being at the target made the move read as already
done, and the whole actuation was skipped, the arrival call with it. The one
call that failed was the one call the retry would not make again, so the device
never learned about a plate sitting on it and no later attempt ever told it.
"""

import asyncio
from collections.abc import AsyncGenerator

import orca.orca as orca
from orca.events.event_bus import EventBus
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placeable_interface import IPlateMover
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.status_enums import RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext

from tests.mock import UniversalMockDevice
from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    wait_for_paused_thread,
    wire_system_map,
)


class _DeviceThatRefusesTheFirstArrival(UniversalMockDevice):
    """Its arrival hook raises once, the way a real device call can."""

    def __init__(self, name: str, site_names: list[str]) -> None:
        super().__init__(name, site_names=site_names)
        self.refuse_next_arrival = True
        self.arrivals = 0

    async def _do_notify_placed(
        self, labware: LabwareInstance, mover: IPlateMover,
        target: str | None = None,
    ) -> None:
        self.arrivals += 1
        if self.refuse_next_arrival:
            self.refuse_next_arrival = False
            raise RuntimeError("Simulated device fault while accepting the plate")
        await super()._do_notify_placed(labware, mover, target)


async def _build() -> tuple[SystemRuntime, WorkflowTemplate, _DeviceThatRefusesTheFirstArrival]:
    device = _DeviceThatRefusesTheFirstArrival("dev1", site_names=["site-1"])
    arm = create_test_transporter("robot1", ["dev1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(arm)
    pool = ResourcePool("dev1", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"dev1": device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate])
    async def shake(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shake

    pad1 = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad1, end=pad1)
    async def only_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield shake_method

    @orca.workflow(name="arrival_wf")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(only_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[plate],
        resources_registry=registry, system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    return (
        SystemRuntime(builder.get_system(), event_bus=event_bus),
        workflow, device,
    )


async def test_a_retry_tells_the_target_what_the_failed_call_never_did() -> None:
    runtime, workflow, device = await _build()
    await runtime.start()
    try:
        record = await runtime.submit_workflow(
            workflow.name, mode=WorkflowRunMode.PURE_SIM,
        )
        paused = await wait_for_paused_thread(runtime, record.id)
        assert device.arrivals == 1

        runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=30.0)

        assert status.status == ExecutionState.COMPLETED
        assert device.arrivals == 2, (
            "the retry must make the arrival call again; it is the only call "
            f"that failed, and it ran {device.arrivals} time(s)"
        )
    finally:
        await runtime.shutdown()
