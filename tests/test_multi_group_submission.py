"""T6c: multi-group submissions produce N independent lineages.

One submission with 3 LabwareGroups should spawn 3 entry threads (one per
group), each tagged with its group_id + submission_id. The group-aware
registry keys slots by (template, group_id, submission_id), so receiver
threads spawned reactively stay scoped to their originating group.
"""

from collections.abc import AsyncGenerator
from uuid import uuid4

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.runtime.run_modes import WorkflowRunMode
from tests.test_helpers import (
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
    wait_until,
    wire_system_map,
)


async def _build_simple_system() -> tuple[ISystem, WorkflowTemplate]:
    device = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("sample_journey")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map, devices={"shaker1": device}, pads=["pad1"],
    )

    @orca.action(device=pool, inputs=[plate])
    async def shake_action(ctx: object) -> None:
        await ctx.device().shake(duration=1, speed=500)

    async def _method_gen(ctx: object) -> AsyncGenerator[object, None]:
        yield shake_action
    method = MethodTemplate("shake", func=_method_gen)

    pad = system_map.get_location("pad1")

    async def _thread_gen(ctx: object) -> AsyncGenerator[MethodTemplate, None]:
        yield method
    thread = ThreadTemplate(
        labware_template=plate, start=pad, end=pad, func=_thread_gen,
    )

    workflow = WorkflowTemplate("mg_workflow")
    workflow.add_thread(thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="mg_system", description="",
        labwares=[plate], resources_registry=registry, system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    return builder.get_system(), workflow


async def test_three_groups_spawn_three_entry_threads_with_distinct_ids() -> None:
    """Submitting 3 groups produces 3 entry-thread instances, each tagged
    with its group_id and the shared submission_id.

    Focus: thread creation correctness at submit time. Execution is not
    awaited because the single-pad fixture can't run 3 plates concurrently
    (reservation conflict on pad1). A richer fixture lands in T6h.
    """
    system, workflow = await _build_simple_system()
    runtime = SystemRuntime(system)
    await runtime.start()

    groups = tuple(
        LabwareGroup(
            id=str(uuid4()),
            members=(LabwareGroupMember(thread_template_name="sample_journey"),),
        )
        for _ in range(3)
    )
    submission = await runtime.submit(workflow, groups=groups, mode=WorkflowRunMode.PURE_SIM)

    await wait_until(
        lambda: runtime._executions[submission.execution_id].executing_workflow is not None,
        timeout=10.0,
    )

    execution = runtime._executions[submission.execution_id]
    assert execution.executing_workflow is not None
    threads = execution.executing_workflow.threads
    assert len(threads) == 3, (
        f"Expected 3 entry threads (one per group); got {len(threads)}"
    )

    group_ids_seen = {t.thread_instance.group_id for t in threads}
    expected_group_ids = {g.id for g in groups}
    assert group_ids_seen == expected_group_ids

    for t in threads:
        assert t.thread_instance.submission_id == submission.id

    await runtime.shutdown()
