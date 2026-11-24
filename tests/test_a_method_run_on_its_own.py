"""A method run on its own through `submit_method`: what the method body emits, plates that converge, and refusals."""
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

import pytest

import orca.orca as orca
from orca.config import OrcaConfig
from orca.events.event_bus import EventBus
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.resource_registry import ResourceRegistry
from orca.runtime.execution_record import ExecutionState
from orca.state.ops_store import SYSTEM_ID
from orca.state.records import DeclaredTracking
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_map import SystemMap
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.a_method_on_its_own import MethodRun, a_method_run_on_its_own
from tests.memory_activity import memory_labware_service, memory_unsaved_positions
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.mock import UniversalMockDevice
from tests.test_helpers import create_test_plate_template, create_test_transporter, wire_system_map

_PADS = ["pad_a", "pad_b", "pad_c"]
_PLATES = ["plate_a", "plate_b", "plate_c"]


async def _one_device(pads: list[str], sites: list[str] | None = None) -> tuple[ResourcePool, ResourceRegistry, SystemMap]:
    device = UniversalMockDevice("shaker1", site_names=sites)
    registry = ResourceRegistry()
    registry.add_resource(create_test_transporter("robot1", ["shaker1", *pads]))
    registry.add_resource(device)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=pads)
    return pool, registry, system_map


async def test_what_the_method_body_emits_reaches_its_action_s_wait_for() -> None:
    pool, registry, system_map = await _one_device(["pad1"])
    plate = create_test_plate_template("plate_96")
    received: list[str | None] = []

    @orca.action(device=pool, inputs=[plate])
    async def wait_action(ctx: ActionContext) -> None:
        value, _data = await ctx.wait_for("phase", timeout=5)
        received.append(value)

    @orca.method
    async def emit_then_act(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        await ctx.emit("phase", value="ready")
        yield wait_action

    async with a_method_run_on_its_own(
        registry, system_map, [plate], "emit_then_act", start={"plate_96": "pad1"}, end={"plate_96": "pad1"},
    ) as run:
        assert run.ended.status is ExecutionState.COMPLETED, run.ended

    assert received == ["ready"]


async def test_a_run_that_names_no_labware_is_refused() -> None:
    pool, registry, system_map = await _one_device(["pad1"])
    plate = create_test_plate_template("plate_96")

    @orca.action(device=pool, inputs=[plate])
    async def some_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def some_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield some_action

    @orca.workflow(name="holds_some_method")
    def holds(wf: WorkflowContext) -> None:
        return None

    builder = SdkToSystemBuilder(
        name="t", description="", labwares=[plate], resources_registry=registry, system_map=system_map,
        workflows=[holds], event_bus=EventBus(), config=OrcaConfig(), ops_history_store=memory_ops_store(),
        variable_store=memory_variable_store(), labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    runtime = await memory_runtime(builder.get_system())
    await runtime.start()
    try:
        with pytest.raises(ValueError, match="names no labware"):
            await runtime.submit_method("holds_some_method", "some_method", {}, {}, mode=WorkflowRunMode.CHECK_RUN)
    finally:
        await runtime.shutdown(confirm=True)


@asynccontextmanager
async def _three_plates_on_one_shared_action() -> AsyncIterator[tuple[MethodRun, list[str]]]:
    """Three plates, each from its own pad, are the inputs of one action that declares a well on each."""
    pool, registry, system_map = await _one_device(_PADS, sites=["site-1", "site-2", "site-3"])
    plates = [create_test_plate_template(name) for name in _PLATES]
    ran: list[str] = []

    @orca.action(device=pool, inputs=[*plates], declares=DeclaredTracking(wells_used={name: ["A1"] for name in _PLATES}))
    async def shared_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)
        ran.append("ran")

    @orca.method
    async def shared_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shared_action

    async with a_method_run_on_its_own(
        registry, system_map, plates, "shared_method", start=dict(zip(_PLATES, _PADS)), end=dict(zip(_PLATES, _PADS)),
    ) as run:
        assert run.ended.status is ExecutionState.COMPLETED, run.ended
        yield run, ran


async def test_three_converging_plates_share_one_action_whose_records_cover_all_three() -> None:
    async with _three_plates_on_one_shared_action() as (run, ran):
        records = await run.system.ops_history.for_execution(run.ended.id).records()
        action_records = [r for r in records if r.thread_id != SYSTEM_ID]
        affected = {name for rec in action_records for op in rec.operations for name in op.affected_labware}
        instances = run.labware

        assert ran == ["ran"]
        assert action_records and all(rec.thread_id for rec in action_records)
        assert {instances[name].name for name in _PLATES} <= affected


async def test_each_converging_plate_travels_from_its_pad_to_the_device_and_back() -> None:
    async with _three_plates_on_one_shared_action() as (run, _ran):
        instances = run.labware
        for plate_name, pad in zip(_PLATES, _PADS):
            history = run.system.labware_location_service.get_history(instances[plate_name]).get_history()
            journey = [location.name for location in history]

            assert journey[0] == pad and journey[-1] == pad, journey
            assert any(name.startswith("shaker1/") for name in journey), journey
