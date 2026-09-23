"""Tests for ThreadFacade.spawn_thread / SystemRuntime.spawn_thread_in_execution.

Operator-driven thread spawn is the recovery path for AUTO_SPAWN_FAILED
incidents (the automatic spawn machinery could not find a matching
template). These tests drive the facade directly.
"""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest

import orca.orca as orca
from orca.events.event_bus import EventBus
from orca.runtime.danger import ConfirmationRequired
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.system_runtime import SystemRuntime
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.sdk.system import SystemMap
from orca.system.resource_registry import ResourceRegistry
from orca.runtime.run_modes import WorkflowRunMode
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.mock import UniversalMockDevice
from tests.test_helpers import (
    execution_outcome,
    create_test_plate_template,
    create_test_transporter,
    wait_until,
    wire_system_map,
)


async def _build_spawn_system() -> tuple[SystemRuntime, Any, str]:
    dev = UniversalMockDevice("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1", "pad2"])
    plate_main = create_test_plate_template("plate_main")
    plate_spawned = create_test_plate_template("plate_spawned")

    registry = ResourceRegistry()
    registry.add_resource(dev)
    registry.add_resource(transporter)
    from orca.resource_models.resource_pool import ResourcePool
    pool = ResourcePool("shaker1", [dev])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": dev}, pads=["pad1", "pad2"])

    @orca.action(device=pool, inputs=[plate_main])
    async def main_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.action(device=pool, inputs=[plate_spawned])
    async def spawned_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def main_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield main_action

    @orca.method
    async def spawned_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield spawned_action

    pad1 = system_map.get_location("pad1")
    pad2 = system_map.get_location("pad2")

    @orca.thread(labware=plate_main, start=pad1, end=pad1)
    async def main_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield main_method

    @orca.thread(labware=plate_spawned, start=pad2, end=pad2)
    async def spawned_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield spawned_method

    @orca.workflow(name="spawn_test_workflow")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(main_thread)
        wf.thread(spawned_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="spawn_test_system", description="",
        labwares=[plate_main, plate_spawned],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    runtime = SystemRuntime(system, event_bus=event_bus)
    return runtime, workflow, "spawned_thread"


@pytest.mark.asyncio
async def test_spawn_thread_creates_and_starts_thread() -> None:
    """spawn_thread adds a new executing thread that runs to completion."""
    runtime, workflow, template_name = await _build_spawn_system()
    await runtime.start()

    submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
    execution_id = submission.execution_id
    await wait_until(lambda: len(runtime.list_threads(execution_id)) >= 1, timeout=15.0)

    before = {t.id for t in runtime.list_threads(execution_id)}
    snap = await runtime.threads.spawn_thread(
        execution_id, template_name, confirm=True,
    )
    assert snap.id not in before, "spawn_thread must return a fresh thread id"

    after = {t.id for t in runtime.list_threads(execution_id)}
    assert snap.id in after, "spawned thread must appear in list_threads"

    await execution_outcome(runtime, submission, timeout=15.0)
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_spawn_thread_requires_confirm() -> None:
    """spawn_thread refuses without confirm=True, raising ConfirmationRequired."""
    runtime, workflow, template_name = await _build_spawn_system()
    await runtime.start()

    submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
    await wait_until(lambda: len(runtime.list_threads(submission.execution_id)) >= 1, timeout=10.0)

    with pytest.raises(ConfirmationRequired):
        await runtime.threads.spawn_thread(
            submission.execution_id, template_name,
        )
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_spawn_thread_unknown_template_raises_keyerror() -> None:
    """Unknown template name -> KeyError (surfaced as 404 at the HTTP edge)."""
    runtime, workflow, _ = await _build_spawn_system()
    await runtime.start()

    submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
    await wait_until(lambda: len(runtime.list_threads(submission.execution_id)) >= 1, timeout=10.0)

    with pytest.raises(KeyError):
        await runtime.threads.spawn_thread(
            submission.execution_id, "no_such_template", confirm=True,
        )
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_spawn_thread_unknown_execution_raises_keyerror() -> None:
    runtime, _, template_name = await _build_spawn_system()
    await runtime.start()

    with pytest.raises(KeyError):
        await runtime.threads.spawn_thread(
            "no-such-execution", template_name, confirm=True,
        )
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_spawn_thread_rejects_wrong_type_labware() -> None:
    """Passing a labware_id whose template doesn't match the thread
    template's expected labware_template raises ValueError.

    The recovery path is where operators are most likely to grab the
    wrong labware id (e.g. pinning a tip rack to a sample-plate thread).
    The ValueError stops the mix-up before the wrong-typed instance
    reaches action dispatch."""
    runtime, workflow, template_name = await _build_spawn_system()
    # Reset the labware store explicitly so register() persists and
    # spawn_thread can look the instance up by id.
    runtime._labware_store = InMemoryLabwareStore()
    from orca.runtime.facades.labware import LabwareFacade
    runtime._labware = LabwareFacade(runtime.system, runtime._labware_store, runtime)
    await runtime.start()

    submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
    execution_id = submission.execution_id
    await wait_until(lambda: len(runtime.list_threads(execution_id)) >= 1, timeout=10.0)

    # Register a labware of the WRONG template for the `spawned_thread`
    # target (spawned_thread expects plate_spawned; we register one of
    # plate_main instead).
    snap = await runtime.labware.register(
        template_name="plate_main",
        barcode="MISPICK",
        confirm=True,
    )

    with pytest.raises(ValueError, match="incompatible"):
        await runtime.threads.spawn_thread(
            execution_id, template_name,
            labware_id=snap.id,
            confirm=True,
        )
    await runtime.shutdown()
