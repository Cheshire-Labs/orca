"""A named plate on another thread's start is only let through if its own start will be free.

Two named plates each standing on the other's start would wait for each other forever, so the
submission is refused. A chain that ends at a free start runs.
"""
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.config import OrcaConfig
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.errors import StartLocationsOccupiedError
from orca.runtime.system_runtime import SystemRuntime, _moves_off
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.labware_group import BarcodeAcquisition, LabwareGroup, LabwareGroupMember
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import ActionTemplate
from orca.workflow_models.submission import ResolvedAcquisition
from orca.workflow_models.thread_context import ThreadContext
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.memory_activity import memory_labware_service, memory_unsaved_positions
from tests.test_helpers import (
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
    execution_outcome,
    wire_system_map,
)


async def _runtime() -> tuple[SystemRuntime, WorkflowTemplate]:
    device = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1", "pad2", "pad3"])
    plate_a = create_test_plate_template("plate_a")
    plate_b = create_test_plate_template("plate_b")
    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1", "pad2", "pad3"])

    @orca.action(device=pool, inputs=[plate_a])
    async def shake_a(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_a

    pad1 = system_map.get_location("pad1")
    pad2 = system_map.get_location("pad2")

    @orca.thread(labware=plate_a, start=pad1, end=pad1)
    async def thread_a(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield shake_method

    @orca.thread(labware=plate_b, start=pad2, end=pad2)
    async def thread_b(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        return
        yield shake_method

    workflow = WorkflowTemplate("two_starts")
    workflow.add_thread(thread_a, is_start=True)
    workflow.add_thread(thread_b, is_start=True)
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[plate_a, plate_b], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=EventBus(), config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    runtime = await memory_runtime(builder.get_system())
    await runtime.start()
    return runtime, workflow


def _group(a_barcode: str, b_barcode: str) -> list[LabwareGroup]:
    return [LabwareGroup(id="g1", members=(
        LabwareGroupMember(thread_template_name="thread_a", acquisition=BarcodeAcquisition(barcode=a_barcode)),
        LabwareGroupMember(thread_template_name="thread_b", acquisition=BarcodeAcquisition(barcode=b_barcode)),
    ))]


async def test_two_named_plates_on_each_others_starts_are_refused() -> None:
    runtime, workflow = await _runtime()
    try:
        await runtime.labware.register("plate_a", location="pad2", barcode="A", confirm=True)
        await runtime.labware.register("plate_b", location="pad1", barcode="B", confirm=True)
        with pytest.raises(StartLocationsOccupiedError):
            await runtime.submit(workflow, groups=_group("A", "B"), mode=WorkflowRunMode.SIM)
    finally:
        await runtime.shutdown()


async def test_a_chain_of_named_plates_that_ends_at_a_free_start_runs() -> None:
    runtime, workflow = await _runtime()
    try:
        await runtime.labware.register("plate_a", location="pad2", barcode="A", confirm=True)
        await runtime.labware.register("plate_b", location="pad3", barcode="B", confirm=True)
        submission = await runtime.submit(workflow, groups=_group("A", "B"), mode=WorkflowRunMode.SIM)
        assert (await execution_outcome(runtime, submission, timeout=30.0)).status == "completed"
    finally:
        await runtime.shutdown()


async def test_a_joined_plate_whose_thread_is_past_its_start_is_carried_off_whatever_holds_that_start() -> None:
    """Its thread already left the start, so the start's occupant no longer matters; only the next move does."""
    runtime, workflow = await _runtime()
    try:
        await runtime.labware.register("plate_a", location="pad2", barcode="A", confirm=True)
        await runtime.labware.register("plate_b", location="pad1", barcode="BYSTANDER", confirm=True)
        pad2 = runtime.system.system_map.get_location("pad2")
        plate_a = pad2.labware
        assert plate_a is not None
        claims = [(("g1", "thread_a"), ResolvedAcquisition(labware_instance=plate_a))]

        assert not _moves_off(workflow, claims, plate_a.id, pad2, frozenset())
        assert _moves_off(workflow, claims, plate_a.id, pad2, frozenset({plate_a.id}))
    finally:
        await runtime.shutdown()
