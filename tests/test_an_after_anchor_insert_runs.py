"""The operator flow the After anchor exists for: pause, insert, resume.

An operator parked on a failed action inserts an action to run after it. Two
things stopped that from happening. The lane only anchored an item that already
had an insert registered against it when it was consumed, and the action being
anchored to had of course already been consumed. And the eager peek that
decides whether a method is finished never looked at After anchors, so even a
registered insert on the last action was passed over.

The insert was dropped, and then reported as an anchor whose tag never appeared
on the stream. The operator is told their tag was wrong.
"""

import asyncio

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from orca.sdk.labware import PlateTemplate
from orca.runtime.incident_store import IncidentCategory
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.mutation_position import After
from orca.workflow_models.status_enums import FailurePolicy, RecoveryDecision
from tests.mutation_helpers import wait_for_paused
from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    wire_system_map,
)
from tests.test_mutation_coordinator import (
    TrackingDevice,
    _make_method,
    _make_thread,
)


async def _paused_on_a_failed_tagged_action(*, with_a_rinse: bool = False) -> tuple[
    SystemRuntime, TrackingDevice, ResourcePool, PlateTemplate, str, str
]:
    """A thread error-paused on ``wash``. With ``with_a_rinse`` the method has a
    second action after it, so insert order is observable."""
    device = TrackingDevice("device1")
    device.should_fail_shake = True
    transporter = create_test_transporter("robot1", ["device1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("device1", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

    @orca.action(
        device=pool, inputs=[plate], tag="wash", failure_policy=FailurePolicy.PAUSE
    )
    async def wash(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.action(device=pool, inputs=[plate], tag="rinse")
    async def rinse(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=200)

    pad_loc = system_map.get_location("pad1")
    method = _make_method(
        "the_method", [wash, rinse] if with_a_rinse else [wash]
    )
    workflow = WorkflowTemplate("test_workflow")
    workflow.add_thread(_make_thread(plate, pad_loc, pad_loc, [method]), is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[plate],
        resources_registry=registry, system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    runtime = SystemRuntime(builder.get_system(), event_bus=event_bus)
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    paused_id = await wait_for_paused(runtime, record.id)
    device.should_fail_shake = False
    return runtime, device, pool, plate, record.id, paused_id


async def test_an_insert_after_the_action_the_operator_is_paused_on_runs() -> None:
    runtime, device, pool, plate, execution_id, paused_id = (
        await _paused_on_a_failed_tagged_action()
    )

    @orca.action(device=pool, inputs=[plate], tag="extra_seal")
    async def extra(ctx: ActionContext) -> None:
        await ctx.device().seal(temperature=180, duration=1)

    runtime.system.insert_action(paused_id, extra, where=After("wash"))
    runtime.recover_thread(execution_id, paused_id, RecoveryDecision.RETRY)
    status = await asyncio.wait_for(runtime.wait(execution_id), timeout=30.0)

    assert status.status == ExecutionState.COMPLETED
    assert device.seal_count == 1, "the inserted action never ran"
    assert await runtime.incidents.list(
        category=IncidentCategory.UNRESOLVED_ANCHOR_INSERT
    ) == [], "the anchor ran, so nothing should be reported as unmatched"
    await runtime.shutdown()


async def test_an_insert_after_a_tag_that_never_runs_is_still_reported() -> None:
    """The guard: the drop report is right when the anchor really is absent."""
    runtime, device, pool, plate, execution_id, paused_id = (
        await _paused_on_a_failed_tagged_action()
    )

    @orca.action(device=pool, inputs=[plate], tag="extra_seal")
    async def extra(ctx: ActionContext) -> None:
        await ctx.device().seal(temperature=180, duration=1)

    runtime.system.insert_action(paused_id, extra, where=After("no_such_tag"))
    runtime.recover_thread(execution_id, paused_id, RecoveryDecision.RETRY)
    status = await asyncio.wait_for(runtime.wait(execution_id), timeout=30.0)

    assert status.status == ExecutionState.COMPLETED
    assert device.seal_count == 0
    incidents = await runtime.incidents.list(
        category=IncidentCategory.UNRESOLVED_ANCHOR_INSERT
    )
    assert len(incidents) == 1
    await runtime.shutdown()


async def test_an_insert_lands_after_its_anchor_and_before_what_follows() -> None:
    """Order, not just arrival: an insert after the first of two actions runs
    between them, not at the end."""
    runtime, device, pool, plate, execution_id, paused_id = (
        await _paused_on_a_failed_tagged_action(with_a_rinse=True)
    )

    @orca.action(device=pool, inputs=[plate], tag="extra_seal")
    async def extra(ctx: ActionContext) -> None:
        await ctx.device().seal(temperature=180, duration=1)

    runtime.system.insert_action(paused_id, extra, where=After("wash"))
    runtime.recover_thread(execution_id, paused_id, RecoveryDecision.RETRY)
    status = await asyncio.wait_for(runtime.wait(execution_id), timeout=30.0)

    assert status.status == ExecutionState.COMPLETED
    ran = [c.command for c in device.calls if c.params.get("succeeded", True)]
    assert ran == ["shake", "seal", "shake"], f"insert landed out of order: {ran}"
    await runtime.shutdown()


async def test_an_insert_after_a_step_that_has_not_run_yet_still_works() -> None:
    """The case the peek half alone fixes: the anchor is the LAST action and is
    still pending when the operator asks."""
    runtime, device, pool, plate, execution_id, paused_id = (
        await _paused_on_a_failed_tagged_action(with_a_rinse=True)
    )

    @orca.action(device=pool, inputs=[plate], tag="extra_seal")
    async def extra(ctx: ActionContext) -> None:
        await ctx.device().seal(temperature=180, duration=1)

    runtime.system.insert_action(paused_id, extra, where=After("rinse"))
    runtime.recover_thread(execution_id, paused_id, RecoveryDecision.RETRY)
    status = await asyncio.wait_for(runtime.wait(execution_id), timeout=30.0)

    assert status.status == ExecutionState.COMPLETED
    ran = [c.command for c in device.calls if c.params.get("succeeded", True)]
    assert ran == ["shake", "shake", "seal"], f"insert landed out of order: {ran}"
    await runtime.shutdown()
