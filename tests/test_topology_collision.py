"""Topology x gateway startup validator behavior at SystemRuntime.start.

Kind drift between topology and gateway is advisory (logs warning, accepts
startup). The safety contract is interface superset, enforced when the
runtime composes topology + connection cards via ``DeviceRegistryImpl``.
"""

from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.registries import NullGatewayRegistry
from orca.runtime.runtime_interface import IGatewayRegistry
from orca.runtime.status_models import GatewayDeviceEntry
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

    workflow = WorkflowTemplate("collision_test_workflow")
    workflow.add_thread(plate_thread, is_start=True)

    builder = SdkToSystemBuilder(
        name="collision_test",
        description="",
        labwares=[plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=EventBus(),
    )
    await builder.bind_labwares()
    return builder.get_system()


async def test_start_succeeds_when_gateway_matches_topology() -> None:
    system = await _build_system()
    # Read the topology-declared kind first via a discardable runtime.
    probe = SystemRuntime(system, gateway_registry=NullGatewayRegistry())
    expected = probe.topology.get_device("shaker1")
    assert expected is not None

    gateway = _StubGateway([
        GatewayDeviceEntry(
            name="shaker1",
            driver_class_observed=expected.kind,
            interfaces=expected.interfaces,
            last_heartbeat=datetime.now(timezone.utc),
            connection_id="conn-ok",
            status="ready",
        ),
    ])
    runtime = SystemRuntime(system, gateway_registry=gateway)

    await runtime.start()
    await runtime.shutdown()


async def test_start_logs_warning_on_kind_drift_but_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Kind drift is advisory: log warning, accept startup.

    The safety contract is the interface superset rule. Different kind
    labels can satisfy the same interface contract, so a kind mismatch
    must not block a startup that would dispatch correctly at the method
    level.
    """
    system = await _build_system()
    probe = SystemRuntime(system, gateway_registry=NullGatewayRegistry())
    expected = probe.topology.get_device("shaker1")
    assert expected is not None

    gateway = _StubGateway([
        GatewayDeviceEntry(
            name="shaker1",
            driver_class_observed="thermal_shaker",  # different kind label
            interfaces=expected.interfaces,  # but interfaces still satisfy
            last_heartbeat=datetime.now(timezone.utc),
            connection_id="conn-drift",
            status="ready",
        ),
    ])
    runtime = SystemRuntime(system, gateway_registry=gateway)

    with caplog.at_level("WARNING", logger="orca.runtime.system_runtime"):
        await runtime.start()
        await runtime.shutdown()

    assert any(
        "kind drift" in rec.message and "shaker1" in rec.message
        for rec in caplog.records
    ), f"expected kind drift warning, got {[r.message for r in caplog.records]}"


async def test_start_accepts_gateway_only_device_without_collision() -> None:
    """A gateway connection for a name not in topology is acceptable
    (surfaced as in_topology=False on the union view); it does not raise."""
    system = await _build_system()
    gateway = _StubGateway([
        GatewayDeviceEntry(
            name="ghost_device",
            driver_class_observed="GhostDriver",
            interfaces=(),
            last_heartbeat=datetime.now(timezone.utc),
            connection_id="conn-ghost",
            status="ready",
        ),
    ])
    runtime = SystemRuntime(system, gateway_registry=gateway)

    await runtime.start()
    await runtime.shutdown()


async def test_null_gateway_start_succeeds_with_no_connections() -> None:
    system = await _build_system()
    runtime = SystemRuntime(system, gateway_registry=NullGatewayRegistry())

    await runtime.start()
    await runtime.shutdown()
