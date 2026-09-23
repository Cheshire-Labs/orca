"""A plate an operator says has arrived counts as arrived by every reader.

An arrival reaches a waiting action as a location event. A move that actuates
fires PLACED; an operator asserting a position fires INITIALIZED. Both mean the
same thing to an action counting its inputs, and only one of them used to open
the gate.

The cost was a run that stopped without stopping. A shared action needs every
input on the device before it runs; its owner waits on that gate with no
timeout. So an operator who finished a failed move by hand -- the recovery the
move's own error message prescribes -- left the owner waiting for a plate that
was already sitting in front of it, with no pause, no error and nothing on any
surface to say what was missing.
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.events.event_bus import EventBus
from orca.resource_models.location import LabwareLocationEvent, Location
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.transporter import Transporter
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
    create_test_teachpoints,
    seeded_teachpoint_service,
    wait_for_paused_thread,
    wire_system_map,
)

OWNER_SITE = "dev1/site-1"
JOINER_SITE = "dev1/site-2"


class _ArmThatJamsOnOnePlace(Transporter):
    """Fails the place at one position, so a move gets as far as the jaws."""

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


class _DeviceThatCountsItsWork(UniversalMockDevice):
    """Records every shake, so a test can say whether the action ever ran."""

    def __init__(self, name: str, site_names: list[str]) -> None:
        super().__init__(name, site_names=site_names)
        self.shakes = 0

    async def shake(self, duration: int, speed: int) -> None:
        self.shakes += 1
        await super().shake(duration=duration, speed=speed)


async def _build_rendezvous() -> tuple[
    SystemRuntime, WorkflowTemplate, _ArmThatJamsOnOnePlace, _DeviceThatCountsItsWork
]:
    """Two threads converging on one two-input action, one site each.

    ``deck_positions`` pins each plate to its own site so a test can jam the
    place onto one of them and know which plate is stranded.
    """
    device = _DeviceThatCountsItsWork("dev1", site_names=["site-1", "site-2"])
    arm = _ArmThatJamsOnOnePlace("robot1", ["dev1", "pad1", "pad2"])
    owner_plate = create_test_plate_template("owner_plate")
    joiner_plate = create_test_plate_template("joiner_plate")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(arm)
    pool = ResourcePool("dev1", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(
        system_map, devices={"dev1": device}, pads=["pad1", "pad2"],
    )

    @orca.action(
        device=pool,
        inputs=[owner_plate, joiner_plate],
        deck_positions={owner_plate: "site-1", joiner_plate: "site-2"},
    )
    async def condense(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def condense_method(
        ctx: MethodContext,
    ) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield condense

    pad1 = system_map.get_location("pad1")
    pad2 = system_map.get_location("pad2")

    @orca.thread(labware=owner_plate, start=pad1, end=pad1)
    async def owner_thread(
        ctx: ThreadContext,
    ) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield condense_method

    @orca.thread(labware=joiner_plate, start=pad2, end=pad2)
    async def joiner_thread(
        ctx: ThreadContext,
    ) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield orca.join()

    @orca.workflow(name="rendezvous_wf")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(owner_thread)
        wf.thread(joiner_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="",
        labwares=[owner_plate, joiner_plate],
        resources_registry=registry, system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    return (
        SystemRuntime(builder.get_system(), event_bus=event_bus),
        workflow, arm, device,
    )


async def test_a_hand_finished_move_reaches_the_action_waiting_on_it() -> None:
    """The bench failure, end to end.

    The joiner's move onto the device jams with the plate in the jaws. The
    operator does what the recovery verbs prescribe: opens the jaws, says where
    the plate now is, and says CONTINUE. The action both plates were converging
    on must then run.
    """
    runtime, workflow, arm, device = await _build_rendezvous()
    arm.jam_at = JOINER_SITE
    await runtime.start()
    try:
        record = await runtime.submit_workflow(
            workflow.name, mode=WorkflowRunMode.PURE_SIM,
        )
        paused = await wait_for_paused_thread(runtime, record.id)
        assert paused.labware_id is not None

        arm.jam_at = None
        await runtime.labware.release_mover_hold(
            arm.name, JOINER_SITE, confirm=True,
            reason="Opened the jaws and set it on the site by hand.",
        )
        runtime.recover_thread(record.id, paused.id, RecoveryDecision.CONTINUE)

        status = await asyncio.wait_for(runtime.wait(record.id), timeout=30.0)
        assert status.status == ExecutionState.COMPLETED
        assert device.shakes >= 1, (
            "the action both plates converged on never ran: the owner was "
            "still waiting for a plate the operator had already put in front "
            "of it"
        )
    finally:
        await runtime.shutdown()


async def test_an_asserted_arrival_opens_the_gate_like_an_actuated_one() -> None:
    """The unit behind it: INITIALIZED counts, the same as PLACED."""
    from tests.test_labware_presence_gate import _GateProbeAction, _location
    from tests.test_helpers import create_test_labware_instance

    action = _GateProbeAction(missing=[])
    labware = await create_test_labware_instance("plate")
    await action.notify_labware_location_change(
        LabwareLocationEvent.INITIALIZED, _location(), labware,
    )
    assert action.all_labware_is_present.is_set()
