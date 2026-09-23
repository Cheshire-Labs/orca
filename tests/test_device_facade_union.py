"""Tests for DeviceFacade.list_devices: union view across topology + gateway.

Covers:
- NullGatewayRegistry: only topology entries surface, all gateway_connected=False.
- A stub gateway with a matching device: union shows gateway_connected=True
  with last_heartbeat populated.
- A stub gateway with a name not in topology: in_topology=False entry appended.
"""

from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.registries import NullGatewayRegistry
from orca.runtime.runtime_interface import IGatewayRegistry
from orca.runtime.status_models import (
    DeviceUnionEntry,
    GatewayDeviceEntry,
)
from orca.runtime.system_runtime import SystemRuntime
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


class _StubGateway(IGatewayRegistry):
    """In-test gateway registry: explicit list of GatewayDeviceEntry."""

    def __init__(self, entries: list[GatewayDeviceEntry]) -> None:
        self._entries = entries

    async def list_connected(self) -> list[GatewayDeviceEntry]:
        return list(self._entries)

    async def get_gateway_status(self, name: str) -> GatewayDeviceEntry | None:
        for e in self._entries:
            if e.name == name:
                return e
        return None


async def _build_system() -> ISystem:
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

    workflow = WorkflowTemplate("union_test_workflow")
    workflow.add_thread(plate_thread, is_start=True)

    builder = SdkToSystemBuilder(
        name="union_test",
        description="",
        labwares=[plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=EventBus(),
    )
    await builder.bind_labwares()
    return builder.get_system()


def _topology_kind_for(runtime: SystemRuntime, name: str) -> str:
    entry = runtime.topology.get_device(name)
    assert entry is not None
    return entry.kind


async def test_list_devices_with_null_gateway_marks_all_disconnected() -> None:
    system = await _build_system()
    runtime = SystemRuntime(system, gateway_registry=NullGatewayRegistry())

    entries = await runtime.devices.list_devices()

    assert {e.name for e in entries} >= {"shaker1", "robot1"}
    for e in entries:
        assert e.in_topology is True
        assert e.gateway_connected is False
        assert e.last_heartbeat is None
        assert e.gateway_kind is None


async def test_list_devices_marks_gateway_connected_when_present() -> None:
    system = await _build_system()
    # Build runtime first to read driver class for matching gateway entry.
    runtime = SystemRuntime(system, gateway_registry=NullGatewayRegistry())
    shaker_class = _topology_kind_for(runtime, "shaker1")

    now = datetime.now(timezone.utc)
    gateway = _StubGateway([
        GatewayDeviceEntry(
            name="shaker1",
            driver_class_observed=shaker_class,
            interfaces=("IShaker",),
            last_heartbeat=now,
            connection_id="conn-1",
            status="ready",
        ),
    ])
    runtime = SystemRuntime(system, gateway_registry=gateway)

    entries = await runtime.devices.list_devices()
    by_name = {e.name: e for e in entries}

    assert by_name["shaker1"].in_topology is True
    assert by_name["shaker1"].gateway_connected is True
    assert by_name["shaker1"].last_heartbeat == now
    assert by_name["shaker1"].gateway_kind == shaker_class
    assert by_name["shaker1"].status == "ready"

    # Non-connected device still surfaces from topology.
    assert by_name["robot1"].in_topology is True
    assert by_name["robot1"].gateway_connected is False


async def test_list_devices_includes_gateway_only_devices() -> None:
    system = await _build_system()
    now = datetime.now(timezone.utc)
    gateway = _StubGateway([
        GatewayDeviceEntry(
            name="rogue_device",
            driver_class_observed="UnknownDriver",
            interfaces=(),
            last_heartbeat=now,
            connection_id="conn-rogue",
            status="ready",
        ),
    ])
    runtime = SystemRuntime(system, gateway_registry=gateway)

    entries = await runtime.devices.list_devices()
    by_name = {e.name: e for e in entries}

    assert "rogue_device" in by_name
    rogue = by_name["rogue_device"]
    assert rogue.in_topology is False
    assert rogue.gateway_connected is True
    assert rogue.topology_kind is None
    assert rogue.gateway_kind == "UnknownDriver"


async def test_list_devices_returns_typed_entries() -> None:
    system = await _build_system()
    runtime = SystemRuntime(system, gateway_registry=NullGatewayRegistry())

    entries = await runtime.devices.list_devices()
    for e in entries:
        assert isinstance(e, DeviceUnionEntry)
