"""Reproduction: a REUSE_EXISTING stationary thread that starts on a DEVICE
location (staging-bridge resource) and is auto-spawned into a shared method
hangs at CREATED instead of joining.

Models the NEBNext reagent_reservoir: start=("mlstar_1", REUSE_EXISTING),
auto-spawned (wf.thread) when the owner's action declares it as a co-input.
"""

import asyncio
from collections.abc import AsyncGenerator

import orca.orca as orca
from orca.events.event_bus import EventBus
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.spawn import LEAVE_IN_PLACE, REUSE_EXISTING
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext

from tests.mock import UniversalMockDevice
from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    wire_system_map,
)


async def _statuses(runtime: SystemRuntime, eid: str) -> dict[str, str]:
    return {t.name: t.status for t in runtime.list_threads(eid)}


async def test_reuse_existing_on_device_location_does_not_self_collide(caplog) -> None:
    device = UniversalMockDevice("dev1")
    transporter = create_test_transporter("robot1", ["dev1", "pad1"])
    plate_main = create_test_plate_template("plate_main")
    reservoir = create_test_plate_template("reservoir")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("dev1", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(
        system_map, devices={"dev1": device}, pads=["pad1"],
    )

    @orca.action(device=pool, inputs=[plate_main, reservoir])
    async def add_reagent(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=100)

    @orca.method
    async def add_reagent_method(
        ctx: MethodContext,
    ) -> AsyncGenerator[ActionTemplate, None]:
        yield add_reagent

    pad1 = system_map.get_location("pad1")
    dev1 = system_map.resolve_journey_location("dev1")

    @orca.thread(labware=plate_main, start=pad1, end=pad1,
                 contributes_to=["reservoir"])
    async def main_thread(
        ctx: ThreadContext,
    ) -> AsyncGenerator[IMethodTemplate, None]:
        yield add_reagent_method

    @orca.thread(labware=reservoir, start=(dev1, REUSE_EXISTING),
                 end=(dev1, LEAVE_IN_PLACE))
    async def reservoir_thread(
        ctx: ThreadContext,
    ) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join(allows=[add_reagent_method])

    @orca.workflow(name="reuse_device_loc_wf")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(main_thread)
        wf.thread(reservoir_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system",
        description="",
        labwares=[plate_main, reservoir],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    runtime = SystemRuntime(builder.get_system(), event_bus=event_bus)
    await runtime.start()
    with caplog.at_level("WARNING", logger="orca"):
        record = await runtime.submit_workflow(
            "reuse_device_loc_wf", mode=WorkflowRunMode.PURE_SIM)
        eid = record.id
        # Drive the auto-spawn + initialize_labware path. Give the buggy
        # version time to spin: on main the spawned receiver re-stages the
        # reservoir and ManualPlaceSpawn retries on the self-collision,
        # emitting the busy/retry warning repeatedly within this window.
        statuses: dict[str, str] = {}
        for _ in range(12):
            await asyncio.sleep(0.25)
            statuses = await _statuses(runtime, eid)
            if any(n.startswith("reservoir") for n in statuses):
                break

    # The reuse-existing reservoir must actually auto-spawn; otherwise the
    # warning-absence assertion below would pass vacuously (no thread, no
    # warning). Its terminal completion is covered by the deck-site e2e in
    # test_deck_site_as_thread_start.py; this minimal mock harness does not
    # drive the shared action to completion, so CREATED is as far as the
    # reservoir gets here -- the bug vs. fix difference is the spin, not the
    # status.
    assert any(n.startswith("reservoir") for n in statuses), (
        f"reuse-existing reservoir thread was never auto-spawned: {statuses}"
    )

    # The bug: reuse-bind stages the reservoir onto the device, then the
    # spawned receiver's initialize_labware re-stages the SAME instance,
    # set_staged_labware raises DeviceBusyError, and ManualPlaceSpawn spins
    # on the self-collision forever (thread pinned at CREATED). The signature
    # is a "busy with <X>; retrying for <X>" warning naming one instance.
    self_collisions = [
        r.getMessage() for r in caplog.records
        if "ManualPlaceSpawn (sim)" in r.getMessage() and "busy" in r.getMessage()
    ]
    assert not self_collisions, (
        "reuse-existing thread re-acquired its own already-placed labware "
        f"(self-collision in initialize_labware): {self_collisions[:3]}"
    )
