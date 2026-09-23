"""DeviceFacade per-name reads AND writes must accept Transporter as well as Device.

`runtime_device_get`, `runtime_device_capabilities`, and
`runtime_device_introspection` raised ``ValueError("Resource X is not an
Equipment resource")`` when called on a transporter, even though
`runtime_device_list` lists transporters as runtime devices. The contract fix
is symmetric: per-name reads accept either kind.

Round-7 / S4 extension (2026-05-20): the same symmetry must hold for
``DeviceFacade.initialize``. Pre-fix the write path called
``self._system.get_device(...)`` directly and raised the same Equipment-only
``ValueError`` for a transporter, which escaped ``InitializeDeviceOperation``'s
typed-error arms (it catches only ``KeyError`` / ``RuntimeError``) and surfaced
as a generic ``internal_error`` on the MCP / REST wire. Reported by Claude
Desktop calling ``operations_initialize_device(device_name="ddr_1")``.
"""

from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.registries import NullGatewayRegistry
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

    workflow = WorkflowTemplate("device_facade_transporter_test_workflow")
    workflow.add_thread(plate_thread, is_start=True)

    builder = SdkToSystemBuilder(
        name="device_facade_transporter_test",
        description="",
        labwares=[plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=EventBus(),
    )
    await builder.bind_labwares()
    return builder.get_system()


async def test_get_device_status_returns_snapshot_for_transporter() -> None:
    system = await _build_system()
    runtime = SystemRuntime(system, gateway_registry=NullGatewayRegistry())

    snapshot = runtime.devices.get_device_status("robot1")

    assert snapshot.name == "robot1"
    assert snapshot.type_name == "Transporter"


async def test_get_device_introspection_returns_introspection_for_transporter() -> None:
    system = await _build_system()
    runtime = SystemRuntime(system, gateway_registry=NullGatewayRegistry())

    introspection = runtime.devices.get_device_introspection("robot1")

    assert introspection.name == "robot1"
    assert introspection.type == "SimTransporterDriver"


async def test_get_supported_commands_returns_descriptors_for_transporter() -> None:
    system = await _build_system()
    runtime = SystemRuntime(system, gateway_registry=NullGatewayRegistry())

    descriptors = runtime.devices.get_supported_commands("robot1")

    # No command interface bridges to an invokable capability on a transporter,
    # so the surface is exactly empty -- no device capabilities leak across.
    assert descriptors == []


async def test_unknown_device_name_raises_keyerror() -> None:
    system = await _build_system()
    runtime = SystemRuntime(system, gateway_registry=NullGatewayRegistry())

    with pytest.raises(KeyError):
        runtime.devices.get_device_status("does_not_exist")


@pytest.mark.asyncio
async def test_initialize_routes_to_transporter() -> None:
    """`runtime.devices.initialize` accepts a transporter name (S4 regression).

    Pre-fix this raised ``ValueError("Resource robot1 is not an Equipment
    resource")`` from ``ResourceRegistry.get_device``, escaped the operation's
    typed-error arms, and surfaced on the wire as ``internal_error``.
    """
    system = await _build_system()
    runtime = SystemRuntime(system, gateway_registry=NullGatewayRegistry())

    # Should not raise -- transporter init must route through `_resolve`,
    # not `_system.get_device`.
    await runtime.devices.initialize("robot1", confirm=True)


@pytest.mark.asyncio
async def test_initialize_routes_to_device() -> None:
    """`runtime.devices.initialize` still works for Equipment Devices.

    Belt-and-suspenders on the `_resolve` swap: equipment path must keep
    working after the refactor.
    """
    system = await _build_system()
    runtime = SystemRuntime(system, gateway_registry=NullGatewayRegistry())

    await runtime.devices.initialize("shaker1", confirm=True)


@pytest.mark.asyncio
async def test_initialize_unknown_raises_keyerror() -> None:
    """Unknown device name from initialize surfaces as KeyError, not ValueError.

    `InitializeDeviceOperation` translates `KeyError` to `not_found`. A leak
    of any other exception type (e.g. ValueError) would regress to the
    pre-fix `internal_error` envelope.
    """
    system = await _build_system()
    runtime = SystemRuntime(system, gateway_registry=NullGatewayRegistry())

    with pytest.raises(KeyError):
        await runtime.devices.initialize("does_not_exist", confirm=True)
