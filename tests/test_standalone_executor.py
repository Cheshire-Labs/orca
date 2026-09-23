"""Tests for StandaloneMethodExecutor.

Verifies that a single method can be executed outside of a full workflow
using StandaloneMethodExecutor with JoinTemplate-based thread generation.
"""

from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from orca.state.records import DeclaredTracking
from orca.state.ops_store import SYSTEM_ID
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.sdk.system import SystemMap
from orca.system.system import System
from orca.system.executors import StandaloneMethodExecutor
from orca.system.resource_registry import ResourceRegistry
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from tests.mock import UniversalMockDevice
from tests.test_helpers import create_test_plate_template, create_test_transporter, wire_system_map


async def _build_system_with_devices(
    device_names: list[str],
    pad_names: list[str],
    site_names: dict[str, list[str]] | None = None,
) -> tuple[UniversalMockDevice, list[ResourcePool], ResourceRegistry, SystemMap]:
    all_position_ids = device_names + pad_names
    transporter = create_test_transporter("robot1", all_position_ids)

    registry = ResourceRegistry()
    registry.add_resource(transporter)

    pools: list[ResourcePool] = []
    devices_by_name: dict[str, UniversalMockDevice] = {}
    first_device: UniversalMockDevice | None = None
    for name in device_names:
        device = UniversalMockDevice(name, site_names=(site_names or {}).get(name))
        registry.add_resource(device)
        pool = ResourcePool(name, [device])
        registry.add_resource_pool(pool)
        pools.append(pool)
        devices_by_name[name] = device
        if first_device is None:
            first_device = device

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices=devices_by_name, pads=pad_names)

    assert first_device is not None
    return first_device, pools, registry, system_map


class TestStandaloneMethodExecutor:

    @pytest.mark.asyncio
    async def test_basic_single_action_execution(self) -> None:
        """A method with one Shake action completes via the standalone executor."""
        device, pools, registry, system_map = await _build_system_with_devices(
            device_names=["shaker1"],
            pad_names=["pad1"],
        )
        plate = create_test_plate_template("plate_96")
        pool = pools[0]

        executed: list[tuple[int, int]] = []

        @orca.action(device=pool, inputs=[plate])
        async def shake_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)
            executed.append((1, 500))

        @orca.method
        async def shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shake_action

        builder = SdkToSystemBuilder(
            name="standalone_test",
            description="",
            labwares=[plate],
            resources_registry=registry,
            system_map=system_map,
        )
        await builder.bind_labwares()
        system = builder.get_system()

        executor = StandaloneMethodExecutor(
            template=shake_method,
            labware_start_mapping={plate: "pad1"},
            labware_end_mapping={plate: "pad1"},
            system=system,
        )
        await executor.start()

        assert executed == [(1, 500)], "standalone executor must run the method's action body exactly once"

    @pytest.mark.asyncio
    async def test_multi_labware_creates_threads_for_each(self) -> None:
        """Two input labwares produce two threads that share one method execution."""
        _device, pools, registry, system_map = await _build_system_with_devices(
            device_names=["shaker1"],
            pad_names=["pad1", "pad2"],
            site_names={"shaker1": ["site-1", "site-2"]},
        )
        plate_a = create_test_plate_template("plate_a")
        plate_b = create_test_plate_template("plate_b")
        pool = pools[0]

        executed: list[str] = []

        @orca.action(device=pool, inputs=[plate_a, plate_b])
        async def shake_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)
            executed.append("ran")

        @orca.method
        async def shared_shake(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shake_action

        builder = SdkToSystemBuilder(
            name="multi_labware_test",
            description="",
            labwares=[plate_a, plate_b],
            resources_registry=registry,
            system_map=system_map,
        )
        await builder.bind_labwares()
        system = builder.get_system()

        executor = StandaloneMethodExecutor(
            template=shared_shake,
            labware_start_mapping={plate_a: "pad1", plate_b: "pad2"},
            labware_end_mapping={plate_a: "pad1", plate_b: "pad2"},
            system=system,
        )

        await executor.start()

        # Shared action needs BOTH plates and ran once: proof two threads were
        # created and converged (one thread could never satisfy both inputs).
        assert len(executed) == 1, "two labware converge on one shared method execution"

    @pytest.mark.asyncio
    async def test_generator_body_emit_reaches_action_wait_for(self) -> None:
        """A method's generator-body ctx.emit() must work on the standalone path
        as on the live path: it runs during populate, latches on the shared
        registry, and the action's ctx.wait_for() reads it."""
        _device, pools, registry, system_map = await _build_system_with_devices(
            device_names=["shaker1"],
            pad_names=["pad1"],
        )
        plate = create_test_plate_template("plate_96")
        pool = pools[0]

        received: list[str | None] = []

        @orca.action(device=pool, inputs=[plate])
        async def wait_action(ctx: ActionContext) -> None:
            value, _data = await ctx.wait_for("phase", timeout=5)
            received.append(value)

        @orca.method
        async def emit_then_act(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            await ctx.emit("phase", value="ready")
            yield wait_action

        builder = SdkToSystemBuilder(
            name="generator_emit_test",
            description="",
            labwares=[plate],
            resources_registry=registry,
            system_map=system_map,
        )
        await builder.bind_labwares()
        system = builder.get_system()

        executor = StandaloneMethodExecutor(
            template=emit_then_act,
            labware_start_mapping={plate: "pad1"},
            labware_end_mapping={plate: "pad1"},
            system=system,
        )
        await executor.start()

        assert received == ["ready"], (
            "generator-body emit must latch on the shared registry and reach "
            "the action's wait_for on the standalone path"
        )

    async def test_empty_start_map_raises_value_error(self) -> None:
        """An empty start_map is rejected at construction time."""
        _device, pools, registry, system_map = await _build_system_with_devices(
            device_names=["shaker1"],
            pad_names=["pad1"],
        )
        plate = create_test_plate_template("plate_96")

        @orca.action(device=pools[0], inputs=[plate])
        async def some_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.method
        async def some_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield some_action

        builder = SdkToSystemBuilder(
            name="validation_test",
            description="",
            labwares=[plate],
            resources_registry=registry,
            system_map=system_map,
        )
        await builder.bind_labwares()
        system = builder.get_system()

        with pytest.raises(ValueError, match="start_map must not be empty"):
            StandaloneMethodExecutor(
                template=some_method,
                labware_start_mapping={},
                labware_end_mapping={plate: "pad1"},
                system=system,
            )

    async def test_empty_end_map_raises_value_error(self) -> None:
        """An empty end_map is rejected at construction time."""
        _device, pools, registry, system_map = await _build_system_with_devices(
            device_names=["shaker1"],
            pad_names=["pad1"],
        )
        plate = create_test_plate_template("plate_96")

        @orca.action(device=pools[0], inputs=[plate])
        async def some_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.method
        async def some_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield some_action

        builder = SdkToSystemBuilder(
            name="validation_test",
            description="",
            labwares=[plate],
            resources_registry=registry,
            system_map=system_map,
        )
        await builder.bind_labwares()
        system = builder.get_system()

        with pytest.raises(ValueError, match="end_map must not be empty"):
            StandaloneMethodExecutor(
                template=some_method,
                labware_start_mapping={plate: "pad1"},
                labware_end_mapping={},
                system=system,
            )


_THREE_PADS = ["pad_a", "pad_b", "pad_c"]
_THREE_PLATES = ["plate_a", "plate_b", "plate_c"]


async def _run_three_thread_standalone() -> tuple[System, StandaloneMethodExecutor]:
    """Run a shared method with THREE labware threads converging on one device.

    Three entry threads (owner + two contributors) share one action whose
    inputs are all three plates. Exercises the co-labware convergence path and
    returns the built system + executor so callers can read the run's
    OpsHistory and per-labware journeys."""
    _device, pools, registry, system_map = await _build_system_with_devices(
        device_names=["shaker1"],
        pad_names=_THREE_PADS,
        site_names={"shaker1": ["site-1", "site-2", "site-3"]},
    )
    plate_a = create_test_plate_template("plate_a")
    plate_b = create_test_plate_template("plate_b")
    plate_c = create_test_plate_template("plate_c")
    pool = pools[0]

    @orca.action(
        device=pool, inputs=[plate_a, plate_b, plate_c],
        declares=DeclaredTracking(
            wells_used={"plate_a": ["A1"], "plate_b": ["A1"], "plate_c": ["A1"]},
        ),
    )
    async def shared_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shared_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shared_action

    builder = SdkToSystemBuilder(
        name="three_thread_trace_test",
        description="",
        labwares=[plate_a, plate_b, plate_c],
        resources_registry=registry,
        system_map=system_map,
    )
    await builder.bind_labwares()
    system = builder.get_system()

    executor = StandaloneMethodExecutor(
        template=shared_method,
        labware_start_mapping={plate_a: "pad_a", plate_b: "pad_b", plate_c: "pad_c"},
        labware_end_mapping={plate_a: "pad_a", plate_b: "pad_b", plate_c: "pad_c"},
        system=system,
    )
    await executor.start()
    return system, executor


class TestStandaloneMethodExecutorTracing:
    """Three threads converging on one standalone method must each feed the
    same OpsHistory + location service the submit path uses. Pins the wiring
    end-to-end so a regression that silently stops emitting records (the gap
    that hid the action-execution bug) fails loud instead of passing vacuously."""

    @pytest.mark.asyncio
    async def test_three_threads_emit_state_tracking(self) -> None:
        """The shared action's declared state tracking lands in OpsHistory and
        covers all three converging labware."""
        system, executor = await _run_three_thread_standalone()

        records = await system.ops_history.for_execution(executor.execution_id).records()
        action_records = [r for r in records if r.thread_id != SYSTEM_ID]
        assert action_records, (
            "three-thread standalone run emitted no action ops_history records; "
            "the tracking_context never reached the executing action"
        )
        for rec in action_records:
            assert rec.thread_id, "ops_history record has empty thread_id"

        affected = {
            name
            for rec in action_records
            for op in rec.operations
            for name in op.affected_labware
        }
        instances = {lw.template_name: lw for lw in system.labwares}
        expected = {instances[p].name for p in _THREE_PLATES}
        assert expected <= affected, (
            f"state tracking must cover all three converging labware, "
            f"expected {expected}, got {affected}"
        )

    @pytest.mark.asyncio
    async def test_three_threads_each_record_journey(self) -> None:
        """Each of the three labware records its own start -> device -> end journey."""
        system, _executor = await _run_three_thread_standalone()

        instances = {lw.template_name: lw for lw in system.labwares}
        for plate_name, pad_name in zip(_THREE_PLATES, _THREE_PADS):
            labware = instances[plate_name]
            journey = [
                loc.name
                for loc in system.labware_location_service.get_history(labware).get_history()
            ]
            assert journey[0] == pad_name, (
                f"{plate_name} journey must start at {pad_name}, got {journey}"
            )
            assert any(loc.startswith("shaker1/") for loc in journey), (
                f"{plate_name} journey must visit the shared device, got {journey}"
            )
            assert journey[-1] == pad_name, (
                f"{plate_name} journey must end at {pad_name}, got {journey}"
            )
