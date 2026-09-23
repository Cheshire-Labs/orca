"""Tests for SystemTopologyRegistry: walks the System resource registry."""

from collections.abc import AsyncGenerator

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.registries import SystemTopologyRegistry
from orca.runtime.status_models import TopologyDeviceEntry
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from tests.test_helpers import (
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
    wire_system_map,
)


async def _build_topology_system() -> ISystem:
    device = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate])
    async def shake_action(ctx: object) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_method(ctx: object) -> AsyncGenerator[object, None]:
        yield shake_action

    pad_loc = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad_loc, end=pad_loc)
    async def plate_thread(ctx: object) -> AsyncGenerator[MethodTemplate, None]:
        yield shake_method

    workflow = WorkflowTemplate("topology_test_workflow")
    workflow.add_thread(plate_thread, is_start=True)

    builder = SdkToSystemBuilder(
        name="topology_test",
        description="",
        labwares=[plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=EventBus(),
    )
    await builder.bind_labwares()
    return builder.get_system()


async def test_topology_registry_lists_devices_and_transporters() -> None:
    system = await _build_topology_system()
    topology = SystemTopologyRegistry(system)

    entries = topology.list_devices()

    names = {e.name for e in entries}
    assert "robot1" in names
    assert "shaker1" in names
    for e in entries:
        assert isinstance(e, TopologyDeviceEntry)
        assert e.kind


async def test_topology_registry_get_device_known_returns_entry() -> None:
    system = await _build_topology_system()
    topology = SystemTopologyRegistry(system)

    shaker_entry = topology.get_device("shaker1")
    assert shaker_entry is not None
    assert shaker_entry.name == "shaker1"
    assert "shaker1" in shaker_entry.position_ids


async def test_topology_registry_get_device_unknown_returns_none() -> None:
    system = await _build_topology_system()
    topology = SystemTopologyRegistry(system)

    assert topology.get_device("does_not_exist") is None


async def test_topology_registry_transporter_has_no_position_ids() -> None:
    """Transporters reach named positions via the teachpoint store rather
    than being mounted at a single location. The topology entry reflects
    that with an empty position_ids tuple."""
    system = await _build_topology_system()
    topology = SystemTopologyRegistry(system)

    robot_entry = topology.get_device("robot1")
    assert robot_entry is not None
    assert robot_entry.position_ids == ()
