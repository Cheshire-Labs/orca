"""Finishing a move by hand must not send the device anywhere.

A move ends by telling the target it has the plate, and behind that call are
device hooks: a door closes, and a liquid handler that just handed a plate to
one of its own sites parks its gantry. Those belong to an arm that has actually
just let go.

When an operator carries the plate instead, the arm never moved and the hooks
are owed to nobody. Making them anyway parks a gantry seconds after the stall
the operator is standing over, and closes a door on hands that were just inside
it. Worse on the device this recovery is for: a liquid handler moving a plate
between its own sites treats the arrival as a park and nothing else, because it
assumes its own move already relocated the plate. On this path that move is the
one that stalled, so the call moves hardware and records nothing.

So the arrival call is owed by the move that set the plate down, and by no one
else. The refusal that stops the engine touching a device an operator has taken
covers it either way.
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.events.event_bus import EventBus
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placeable_interface import IPlateMover
from orca.resource_models.location import Location
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.transporter import Transporter
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
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
    create_test_teachpoints,
    seeded_teachpoint_service,
    wait_for_paused_thread,
    wait_until,
    wire_system_map,
)

SITE = "dev1/site-1"


class _ArmThatJamsOnOnePlace(Transporter):
    def __init__(self, name: str, position_ids: list[str]) -> None:
        super().__init__(
            name,
            teachpoint_store=seeded_teachpoint_service(
                create_test_teachpoints(position_ids)
            ),
        )
        self.jam_at: str | None = None

    async def place(self, location: Location) -> None:
        if self.jam_at == location.position_id:
            raise RuntimeError("Simulated place failure: arm jammed")
        await super().place(location)


class _DeviceThatCountsItsHooks(UniversalMockDevice):
    """Counts the arrival hook, which is where the door and the park live."""

    def __init__(self, name: str, site_names: list[str]) -> None:
        super().__init__(name, site_names=site_names)
        self.arrival_hooks = 0
        self.refuse_next_arrival = False

    async def _do_notify_placed(
        self, labware: LabwareInstance, mover: IPlateMover,
        target: str | None = None,
    ) -> None:
        self.arrival_hooks += 1
        if self.refuse_next_arrival:
            self.refuse_next_arrival = False
            raise RuntimeError("Simulated device fault while accepting the plate")
        await super()._do_notify_placed(labware, mover, target)


async def _build() -> tuple[
    SystemRuntime, WorkflowTemplate, _ArmThatJamsOnOnePlace, _DeviceThatCountsItsHooks
]:
    device = _DeviceThatCountsItsHooks("dev1", site_names=["site-1"])
    arm = _ArmThatJamsOnOnePlace("robot1", ["dev1", "pad1"])
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

    @orca.workflow(name="hand_placed_wf")
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
        workflow, arm, device,
    )


@pytest.mark.timeout(60)
async def test_a_plate_the_operator_carried_fires_no_device_hook() -> None:
    runtime, workflow, arm, device = await _build()
    arm.jam_at = SITE
    await runtime.start()
    try:
        record = await runtime.submit_workflow(
            workflow.name, mode=WorkflowRunMode.PURE_SIM,
        )
        paused = await wait_for_paused_thread(runtime, record.id)
        assert device.arrival_hooks == 0, "the place jammed; nothing arrived"

        arm.jam_at = None
        await runtime.labware.release_mover_hold(
            arm.name, SITE, confirm=True,
            reason="Opened the jaws and set it on the site by hand.",
        )
        runtime.recover_thread(record.id, paused.id, RecoveryDecision.CONTINUE)
        await asyncio.wait_for(runtime.wait(record.id), timeout=30.0)

        assert device.arrival_hooks == 0, (
            "no arm handed this device anything, so its arrival hook is owed "
            "to nobody. Firing it closes a door and parks a gantry on a plate "
            "somebody placed by hand"
        )
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(60)
async def test_the_owed_arrival_call_refuses_a_device_an_operator_has_taken() -> None:
    """The retry that pays the debt is still a call into a device.

    Its own refusal, because the gate the ordinary move passes is skipped here:
    nothing is being carried, so nothing asks whether the arm may set out.
    """
    runtime, workflow, arm, device = await _build()
    device.refuse_next_arrival = True
    await runtime.start()
    record = await runtime.submit_workflow(
        workflow.name, mode=WorkflowRunMode.PURE_SIM,
    )
    try:
        paused = await wait_for_paused_thread(runtime, record.id)
        assert device.arrival_hooks == 1, "the arrival call ran and raised"

        device.take_external_control()
        runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY)

        def _refused() -> bool:
            return any(
                "external" in (t.last_error or "").lower()
                for t in runtime.get_paused_threads(record.id)
            )

        await wait_until(_refused, timeout=20.0)
        assert device.arrival_hooks == 1, (
            "somebody has their hands in this device; the owed call must wait "
            "for them to give it back"
        )
        paused = runtime.get_paused_threads(record.id)[0]
    finally:
        for still_paused in runtime.get_paused_threads(record.id):
            runtime.recover_thread(
                record.id, still_paused.id, RecoveryDecision.ABORT_THREAD,
            )
        await asyncio.wait_for(runtime.wait(record.id), timeout=30.0)
        await runtime.shutdown()
