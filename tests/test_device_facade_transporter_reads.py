"""DeviceFacade per-name reads AND writes must accept Transporter as well as Device.

`runtime_device_get`, `runtime_device_capabilities`, and
`runtime_device_introspection` raised ``ValueError("Resource X is not an
Equipment resource")`` when called on a transporter, even though
`runtime_device_list` lists transporters as runtime devices. The contract fix
is symmetric: per-name reads accept either kind.

The step an operator's initialize runs after the command resolves either kind
too, and an unknown name answers `device_not_found`.
"""

from orca.config import OrcaConfig
from collections.abc import AsyncGenerator

import pytest

from orca.gateway.controller.exceptions import DeviceUnknownError
from orca.operations.device import InitializeDeviceOperation
from orca.operations.device_models import InitializeDeviceRequest
from orca.operations.protocol import OperationError
from tests.runtime.operator_calls import recording_runner

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from tests.no_devices import NullGatewayRegistry
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
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.memory_activity import memory_unsaved_positions
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service


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
        event_bus=EventBus(), config=OrcaConfig(), ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(),
        unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    return builder.get_system()


async def test_get_device_status_returns_snapshot_for_transporter() -> None:
    system = await _build_system()
    runtime = await memory_runtime(system, gateway_registry=NullGatewayRegistry())

    snapshot = await runtime.devices.get_device_status("robot1")

    assert snapshot.name == "robot1"
    assert snapshot.type_name == "Transporter"


async def test_get_device_introspection_returns_introspection_for_transporter() -> None:
    system = await _build_system()
    runtime = await memory_runtime(system, gateway_registry=NullGatewayRegistry())

    introspection = runtime.devices.get_device_introspection("robot1")

    assert introspection.name == "robot1"
    assert introspection.type == "transporter"


async def test_get_supported_commands_returns_descriptors_for_transporter() -> None:
    system = await _build_system()
    runtime = await memory_runtime(system, gateway_registry=NullGatewayRegistry())

    descriptors = runtime.devices.get_supported_commands("robot1")

    # No command interface bridges to an invokable capability on a transporter,
    # so the surface is exactly empty -- no device capabilities leak across.
    assert descriptors == []


async def test_unknown_device_name_raises_keyerror() -> None:
    system = await _build_system()
    runtime = await memory_runtime(system, gateway_registry=NullGatewayRegistry())

    with pytest.raises(KeyError):
        await runtime.devices.get_device_status("does_not_exist")


@pytest.mark.asyncio
async def test_after_a_lifecycle_verb_a_transporter_is_accepted() -> None:
    """The step after an operator's initialize resolves a transporter as well as a device."""
    system = await _build_system()
    runtime = await memory_runtime(system, gateway_registry=NullGatewayRegistry())

    await runtime.devices.after_lifecycle("robot1", "initialize")
    await runtime.devices.after_lifecycle("shaker1", "initialize")


@pytest.mark.asyncio
async def test_initialize_of_an_unknown_device_is_not_found() -> None:
    """`InitializeDeviceOperation` answers `device_not_found`, not an internal error, for a name nothing knows."""
    system = await _build_system()
    runtime = await memory_runtime(system, gateway_registry=NullGatewayRegistry())
    runner = recording_runner(fails_with=DeviceUnknownError("does_not_exist"))

    with pytest.raises(OperationError) as refused:
        await InitializeDeviceOperation(runtime=runtime, devices=runner).run(
            InitializeDeviceRequest(device_id="does_not_exist"),
        )

    assert refused.value.http_status == 404
