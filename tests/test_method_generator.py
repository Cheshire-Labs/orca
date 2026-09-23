"""Tests for method-as-generator and yield adapter changes.

Step 4: @orca.method becomes an async generator yielding @orca.action templates.
Threads can yield methods OR actions directly.
"""

import asyncio
from typing import AsyncGenerator

import pytest

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import Action, ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate, MethodTemplate
from orca.workflow_models.status_enums import FailurePolicy
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import ThreadTemplate
from orca.runtime.run_modes import WorkflowRunMode
from tests.mock import UniversalMockDevice
from tests.test_helpers import create_test_plate_template, create_test_transporter, execution_outcome, wire_system_map


# ---------------------------------------------------------------------------
# System builder for method generator tests
# ---------------------------------------------------------------------------


async def _build_system_with_thread(
    thread: ThreadTemplate,
    devices: list[UniversalMockDevice],
    plates: list,
    methods: list,
) -> tuple[SystemRuntime, WorkflowTemplate]:
    """Build a minimal system from a thread template and devices."""
    position_ids = [d.name for d in devices] + ["pad1"]
    transporter = create_test_transporter("robot1", position_ids)

    registry = ResourceRegistry()
    for device in devices:
        registry.add_resource(device)
        pool = ResourcePool(device.name, [device])
        registry.add_resource_pool(pool)
    registry.add_resource(transporter)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map, devices={d.name: d for d in devices}, pads=["pad1"],
    )

    workflow = WorkflowTemplate("gen_test")
    workflow.add_thread(thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="",
        labwares=plates, resources_registry=registry,
        system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    runtime = SystemRuntime(system, event_bus=event_bus)
    return runtime, workflow


# ---------------------------------------------------------------------------
# Tests: Method generator yields actions
# ---------------------------------------------------------------------------


class TestMethodGenerator:

    @pytest.mark.asyncio
    async def test_method_generator_yields_actions_in_order(self) -> None:
        """Method generator yields two actions, both execute in order."""
        device = UniversalMockDevice("shaker_1")
        pool = ResourcePool("shaker_1", [device])
        plate = create_test_plate_template("plate_96")

        call_log: list[int] = []
        original_shake = device.shake

        async def tracking_shake(duration: int, speed: int) -> None:
            call_log.append(speed)
            await original_shake(duration, speed)

        device.shake = tracking_shake  # type: ignore[method-assign]

        @orca.action(device=pool, inputs=[plate])
        async def shake_fast(ctx: ActionContext) -> None:
            await ctx.device().shake(1, 500)

        @orca.action(device=pool, inputs=[plate])
        async def shake_slow(ctx: ActionContext) -> None:
            await ctx.device().shake(1, 100)

        @orca.method
        async def two_shakes(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shake_fast
            yield shake_slow

        pad_loc = SystemMap(ResourceRegistry()).get_location("pad1") if False else None  # placeholder
        # Build system properly
        position_ids = ["shaker_1", "pad1"]
        transporter = create_test_transporter("robot1", position_ids)
        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"shaker_1": device}, pads=["pad1"])
        pad_loc = system_map.get_location("pad1")

        async def _thread_gen(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield two_shakes

        thread = ThreadTemplate(
            labware_template=plate, start=pad_loc, end=pad_loc,
            func=_thread_gen,
        )
        workflow = WorkflowTemplate("gen_test")
        workflow.add_thread(thread, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test", description="",
            labwares=[plate], resources_registry=registry,
            system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        runtime = SystemRuntime(builder.get_system(), event_bus=event_bus)

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)

        assert status.status == "completed", f"Got {status.status}: {status.error}"
        assert call_log == [500, 100], f"Expected [500, 100], got {call_log}"
        await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_method_with_generator_func_still_works(self) -> None:
        """MethodTemplate constructed with func= still works (regression)."""
        device = UniversalMockDevice("shaker_1")
        pool = ResourcePool("shaker_1", [device])
        plate = create_test_plate_template("plate_96")

        @orca.action(device=pool, inputs=[plate])
        async def shake_a(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.action(device=pool, inputs=[plate])
        async def shake_b(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=100)

        @orca.method
        async def two_shakes(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shake_a
            yield shake_b

        transporter = create_test_transporter("robot1", ["shaker_1", "pad1"])
        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"shaker_1": device}, pads=["pad1"])
        pad_loc = system_map.get_location("pad1")

        async def _thread_gen(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield two_shakes

        thread = ThreadTemplate(
            labware_template=plate, start=pad_loc, end=pad_loc,
            func=_thread_gen,
        )
        workflow = WorkflowTemplate("decl_test")
        workflow.add_thread(thread, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test", description="",
            labwares=[plate], resources_registry=registry,
            system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        runtime = SystemRuntime(builder.get_system(), event_bus=event_bus)

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)

        assert status.status == "completed", f"Got {status.status}: {status.error}"
        await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_thread_yields_action_directly(self) -> None:
        """Thread generator yields an Action template directly (no method wrapper)."""
        device = UniversalMockDevice("shaker_1")
        pool = ResourcePool("shaker_1", [device])
        plate = create_test_plate_template("plate_96")

        call_log: list[str] = []
        original_shake = device.shake

        async def tracking_shake(duration: int, speed: int) -> None:
            call_log.append("shake")
            await original_shake(duration, speed)

        device.shake = tracking_shake  # type: ignore[method-assign]

        @orca.action(device=pool, inputs=[plate])
        async def shake_it(ctx: ActionContext) -> None:
            await ctx.device().shake(1, 500)

        # thread intentionally yields an action template directly, not a method
        @orca.thread(labware=plate, start="pad1", end="pad1")  # type: ignore[arg-type]
        async def plate_journey(ctx: ThreadContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shake_it

        # Build system -- need to set actual locations on the thread template
        transporter = create_test_transporter("robot1", ["shaker_1", "pad1"])
        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"shaker_1": device}, pads=["pad1"])
        pad_loc = system_map.get_location("pad1")

        # Resolve the string locations against the map (public path).
        plate_journey.resolve_locations(system_map.resolve_journey_location)
        assert pad_loc in plate_journey.end_locations

        workflow = WorkflowTemplate("thread_action_test")
        workflow.add_thread(plate_journey, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test", description="",
            labwares=[plate], resources_registry=registry,
            system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        runtime = SystemRuntime(builder.get_system(), event_bus=event_bus)

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)

        assert status.status == "completed", f"Got {status.status}: {status.error}"
        assert call_log == ["shake"], f"Expected ['shake'], got {call_log}"
        await runtime.shutdown()
