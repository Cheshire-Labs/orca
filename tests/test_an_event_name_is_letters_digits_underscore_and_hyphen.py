"""An author's event name may hold only letters, digits, `_` and `-`, and is refused as early as it can be.

`wf.on` refuses when the workflow is built. `orca.on` and `orca.branch` refuse when the thread reaches that step,
and `ctx.emit` and `ctx.wait_for` refuse when they are called, so the thread pauses with the rule in its error. A good name reaches the event stream as written.
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.config import OrcaConfig
from orca.events.event_bus import EventBus
from orca.events.event_name import InvalidEventNameError
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.sinks import CollectorSink
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service, memory_unsaved_positions
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.mock import UniversalMockDevice
from tests.test_helpers import create_test_plate_template, create_test_transporter, wait_for_paused_threads, wire_system_map

BAD_NAMES = ["qc.done", "qc/done", "qc\\done", "qc done", "", "qc:done", "qc*", "qc#1", "ü"]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_orca_on_and_branch_refuse_a_bad_name(name: str) -> None:
    with pytest.raises(InvalidEventNameError, match="letters, digits"):
        orca.on(name)
    with pytest.raises(InvalidEventNameError, match="letters, digits"):
        orca.branch(name, {"else": []})


@pytest.mark.parametrize("name", ["ACTION..COMPLETED", "ACTION.COMPLETED ", ".x", ""])
def test_wf_on_refuses_a_handler_event_with_an_empty_or_spaced_part(name: str) -> None:
    with pytest.raises(InvalidEventNameError, match="no part is empty"):
        WorkflowTemplate("w").add_event_handler(name, lambda event, context: None)


def test_wf_on_refuses_a_custom_event_whose_name_breaks_the_rule() -> None:
    with pytest.raises(InvalidEventNameError, match="letters, digits"):
        WorkflowTemplate("w").add_event_handler("CUSTOM.qc/done.EMITTED", lambda event, context: None)


@pytest.mark.parametrize("name", ["CUSTOM.qc.done.EMITTED", "CUSTOM.qc_done", "CUSTOM.qc_done.FIRED"])
def test_wf_on_refuses_a_custom_event_that_is_not_custom_name_emitted(name: str) -> None:
    with pytest.raises(InvalidEventNameError, match="CUSTOM.<name>.EMITTED"):
        WorkflowTemplate("w").add_event_handler(name, lambda event, context: None)


@pytest.mark.parametrize("name", [
    "ACTION.COMPLETED", "CUSTOM.qc_done.EMITTED", "COMMAND.arm/gripper.COMPLETED", "SLOT.wf:final%2F1.REJECTED",
])
def test_wf_on_accepts_engine_and_custom_events(name: str) -> None:
    WorkflowTemplate("w").add_event_handler(name, lambda event, context: None)


@pytest.mark.parametrize("name", ["qc_done", "QC-Done-2", "go"])
def test_a_good_name_is_accepted(name: str) -> None:
    assert orca.on(name).event_name == name


async def _run_one_action(body_name: str) -> tuple[SystemRuntime, str, CollectorSink]:
    shaker = UniversalMockDevice("shaker")
    transporter = create_test_transporter("robot1", ["shaker", "pad1"])
    plate = create_test_plate_template("plate")
    registry = ResourceRegistry()
    registry.add_resource(shaker)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker", [shaker])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker": shaker}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate])
    async def emits(ctx: ActionContext) -> None:
        if body_name == "emit_bad":
            await ctx.emit("qc.done")
        elif body_name == "wait_bad":
            await ctx.wait_for("qc/done", timeout=1.0)
        else:
            await ctx.emit("qc_done", value="pass")

    @orca.method
    async def method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield emits

    pad1 = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad1, end=pad1)
    async def journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield method

    @orca.workflow(name="events")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(journey)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="events", description="", labwares=[plate], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=event_bus, config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    runtime = await memory_runtime(builder.get_system(), event_bus=event_bus)
    events = CollectorSink()
    runtime.register_sink(events)
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.CHECK_RUN)
    return runtime, record.id, events


@pytest.mark.parametrize("body", ["emit_bad", "wait_bad"])
async def test_ctx_emit_and_wait_for_refuse_a_bad_name_and_the_action_pauses(body: str) -> None:
    runtime, execution_id, _ = await _run_one_action(body)
    try:
        [paused] = await wait_for_paused_threads(runtime, execution_id)
        assert "letters, digits" in (paused.last_error or ""), paused.last_error
    finally:
        await runtime.shutdown()


async def test_a_good_name_reaches_the_event_stream_as_written() -> None:
    runtime, execution_id, events = await _run_one_action("emit_good")
    try:
        final = await asyncio.wait_for(runtime.wait(execution_id), timeout=10.0)
        assert final.status is ExecutionState.COMPLETED
        assert "CUSTOM.qc_done.EMITTED" in [e.event_name for e in events.events]
    finally:
        await runtime.shutdown()
