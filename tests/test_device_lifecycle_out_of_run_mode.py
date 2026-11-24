"""An out-of-run device lifecycle verb means the REAL device by default, through the operator path.

Out of a run every device verb goes in LIVE, and under LIVE each device runs as
its own sim or live setting, so a device set to sim stays off hardware. No verb
takes a mode of its own. Every verb goes over the operator runner, so each test
reads the mode it was sent under.
"""

import pytest
from pydantic import ValidationError

from orca.config import OrcaConfig
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from pydantic import BaseModel

from orca.operations.device_models import (
    CompareDeckRequest,
    ConnectDeviceRequest,
    DisconnectDeviceRequest,
    InitializeDeviceRequest,
    ReconcileDeckRequest,
)
from orca.operations.operator_device_models import BatchExecuteRequest
from orca.gateway.operator_commands import OperatorDeviceCommands
from orca.operations.device import ConnectDeviceOperation, DisconnectDeviceOperation, InitializeDeviceOperation
from orca.operations.operator_device import DeviceBatchExecuteOperation
from orca.operations.operator_device import TakesTheRuntimeAndTheRunner as _LifecycleOperation

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from tests.no_devices import NullGatewayRegistry
from orca.gateway.device_gateway import DeviceGateway
from orca.gateway.registry.snapshot import DeviceSnapshot
from orca.resource_models.devices import Device
from orca.modes.device_modes import DeviceMode
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from tests.device_modes import set_to
from tests.mock import UniversalMockDevice, UniversalSimDriver
from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    wire_system_map,
)
from tests.gateway_parts import gateway_with
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.memory_activity import memory_unsaved_positions
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service

pytestmark = pytest.mark.out_of_run


class _TrackingDriver(UniversalSimDriver):
    """Records which verbs landed on this driver slot, and the mode each was sent under."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.calls: list[str] = []
        self.modes: list[WorkflowRunMode] = []
        self.device: Device[UniversalSimDriver] | None = None

    async def _note(self, verb: str) -> None:
        self.calls.append(verb)
        assert self.device is not None
        self.modes.append(await self.device.effective_mode())

    async def initialize(self) -> None:
        await self._note("initialize")
        await super().initialize()

    async def connect(self) -> None:
        await self._note("connect")
        await super().connect()

    async def disconnect(self) -> None:
        await self._note("disconnect")
        await super().disconnect()

    async def shake(self, request) -> None:
        await self._note("shake")
        await super().shake(request)


def _connected_shaker() -> DeviceGateway:
    """A gateway where `shaker1` is connected and every command it is sent is recorded and answered."""
    tracker = MagicMock()
    tracker.get_device = AsyncMock(return_value=DeviceSnapshot(
        type="shaker", name="shaker1", interfaces=[], capabilities=[], provides_state=False, methods={},
        site="test", lab="test", workcell=None, status="ready", last_seen=datetime.now(timezone.utc),
    ))
    controller = MagicMock()
    controller.execute_command = AsyncMock(return_value=None)
    controller.fault = MagicMock(return_value=None)
    return gateway_with(controller=controller, tracker=tracker)


def _sent(gateway: DeviceGateway) -> list[tuple[str, WorkflowRunMode]]:
    sent = gateway.controller.execute_command
    assert isinstance(sent, AsyncMock)
    return [(c.kwargs["command"], c.kwargs["effective_mode"]) for c in sent.await_args_list]


async def _build_runtime(
    *, setting: DeviceMode, gateway: DeviceGateway | None = None,
) -> tuple[SystemRuntime, _TrackingDriver]:
    driver = _TrackingDriver("shaker1")
    device = UniversalMockDevice("shaker1", driver=driver)
    set_to(device, "shaker1", setting)
    driver.device = device
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate_oor_mode")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate])
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shake_action

    pad_loc = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad_loc, end=pad_loc)
    async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        del ctx
        yield shake_method

    workflow = WorkflowTemplate("wf_oor_mode")
    workflow.add_thread(plate_thread, is_start=True)

    builder = SdkToSystemBuilder(
        name="oor_mode_system",
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
    system = builder.get_system()
    runtime = await memory_runtime(system, gateway_registry=NullGatewayRegistry(), gateway=gateway)
    return runtime, driver


_LIFECYCLE: dict[str, tuple[type[_LifecycleOperation], type[BaseModel]]] = {
    "initialize": (InitializeDeviceOperation, InitializeDeviceRequest),
    "connect": (ConnectDeviceOperation, ConnectDeviceRequest),
    "disconnect": (DisconnectDeviceOperation, DisconnectDeviceRequest),
}


def _runner(runtime: SystemRuntime, gateway: DeviceGateway) -> OperatorDeviceCommands:
    return OperatorDeviceCommands(gateway, lambda: runtime)


async def _send_the_verb(runtime: SystemRuntime, gateway: DeviceGateway, verb: str) -> None:
    op_class, request_class = _LIFECYCLE[verb]
    await op_class(runtime=runtime, devices=_runner(runtime, gateway)).run(
        request_class.model_validate({"device_id": "shaker1"}),
    )


async def test_initialize_out_of_run_reaches_the_live_driver() -> None:
    gateway = _connected_shaker()
    runtime, live = await _build_runtime(setting=DeviceMode.LIVE, gateway=gateway)

    await _send_the_verb(runtime, gateway, "initialize")

    assert _sent(gateway) == [("initialize", WorkflowRunMode.LIVE)]
    assert live.calls == [], "the verb went past the operator path, its lock and its save"


@pytest.mark.parametrize("request_model", [
    InitializeDeviceRequest, ConnectDeviceRequest, DisconnectDeviceRequest, ReconcileDeckRequest, CompareDeckRequest,
])
def test_a_device_verb_takes_no_mode_of_its_own(request_model: type[BaseModel]) -> None:
    """The world a command goes to is the device's setting and any active run, never the caller's choice."""
    with pytest.raises(ValidationError):
        request_model.model_validate({"device_id": "shaker1", "mode": "CHECK_RUN"})


async def test_a_device_set_to_sim_is_sent_the_verb_as_sim() -> None:
    """A device set to sim never reaches hardware: the verb goes over the wire
    as SIM, and orca-client runs it on its simulator."""
    gateway = _connected_shaker()
    runtime, live = await _build_runtime(setting=DeviceMode.SIM, gateway=gateway)

    await _send_the_verb(runtime, gateway, "initialize")

    assert _sent(gateway) == [("initialize", WorkflowRunMode.SIM)]


async def test_connect_and_disconnect_share_the_live_default() -> None:
    gateway = _connected_shaker()
    runtime, live = await _build_runtime(setting=DeviceMode.LIVE, gateway=gateway)

    await _send_the_verb(runtime, gateway, "connect")
    await _send_the_verb(runtime, gateway, "disconnect")

    assert _sent(gateway) == [("connect", WorkflowRunMode.LIVE), ("disconnect", WorkflowRunMode.LIVE)]


async def test_commands_by_name_reach_the_live_driver() -> None:
    """The command-by-name verb is a device WRITE too: out of a run it goes to the live world."""
    gateway = _connected_shaker()
    runtime, live = await _build_runtime(setting=DeviceMode.LIVE, gateway=gateway)
    runner = _runner(runtime, gateway)

    await DeviceBatchExecuteOperation(runtime=runtime, devices=runner).run(
        BatchExecuteRequest(device_id="shaker1", commands=[{"command": "beep"}], stop_on_error=True),
    )
    await DeviceBatchExecuteOperation(runtime=runtime, devices=runner).run(
        BatchExecuteRequest(
            device_id="shaker1", commands=[{"command": "shaker.shake", "params": {"duration": 1, "speed": 500}}],
         stop_on_error=True),
    )

    assert _sent(gateway) == [("beep", WorkflowRunMode.LIVE), ("shake", WorkflowRunMode.LIVE)]
    assert live.calls == [], "a command by name went past the operator path, its lock and its save"


def test_a_command_by_name_takes_no_mode_of_its_own() -> None:
    with pytest.raises(ValidationError):
        BatchExecuteRequest.model_validate({"device_id": "shaker1", "commands": [], "mode": "CHECK_RUN"})


@pytest.mark.parametrize("setting,world", [
    (DeviceMode.LIVE, WorkflowRunMode.LIVE),
    (DeviceMode.SIM, WorkflowRunMode.SIM),
])
async def test_the_snapshot_names_the_world_the_verb_was_sent_to(
    setting: DeviceMode, world: WorkflowRunMode,
) -> None:
    """One response must not mix two worlds: the snapshot's effective_mode is the mode initialize went under."""
    gateway = _connected_shaker()
    runtime, live = await _build_runtime(setting=setting, gateway=gateway)

    await _send_the_verb(runtime, gateway, "initialize")
    snapshot = await runtime.devices.get_device_status("shaker1")

    assert _sent(gateway) == [("initialize", world)]
    assert snapshot.setting is setting
    assert snapshot.effective_mode is world
