"""A command leaves the same trail whoever sent it.

A workflow action and an operator's own command reach the same driver and move
the same hardware. They used to be read by two pieces of code with two tables
of which commands count, so a command could be fully recorded when a workflow
issued it and invisible when an operator or an AI client issued it. Both read
``orca.state.command_operations`` now, off the typed request the bridge sends
and the operator's surface parses.

What is left different is who asked, which rides on the record as its source.
"""

import pytest
from cheshire_drivers.command_table import COMMANDS
from cheshire_drivers.liquid_handler_models import (
    GetDeckStateRequest,
    PickUpTipsRequest,
    TipPick,
)
from pydantic import BaseModel

from orca.events.event_bus import EventBus
from tests.no_devices import NullGatewayRegistry
from orca.plugins.liquid_handler_interpreter import _REQUEST_BUILDERS, LiquidHandlerInterpreter
from orca.resource_models.tracking_interpreter import DefaultInterpreter
from orca.gateway.operator_recorder import record_operator_command
from orca.modes.run_modes import WorkflowRunMode, mode_scope
from orca.runtime.runtime_interface import ISystemRuntime
from orca.sdk.build import Topology
from orca.sdk.package import build_system
from orca.state import command_operations
from orca.state.command_operations import (
    RECORDED_BY_NAME,
    RecordContext,
    operations_for,
    operations_for_command,
    operations_for_request,
)
from orca.state.ops_store import SYSTEM_ID, OpsHistorySearchQuery
from orca.state.records import (
    DeviceOperation,
    GenericOperationDetails,
    TipPickUpDetails,
    TrackingSource,
)
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from orca.gateway.device_gateway import DeviceGateway
from orca.runtime.startup import build_device_gateway
from orca.gateway.operator_commands import OperatorDeviceCommands
from orca.gateway.registry.snapshot import DeviceSnapshot
from orca.operations.device import ConnectDeviceOperation, DisconnectDeviceOperation, InitializeDeviceOperation
from orca.operations.device_models import ConnectDeviceRequest, DisconnectDeviceRequest, InitializeDeviceRequest
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.mock import UniversalMockDevice
from tests.setup_package import package_stores
from orca.runtime.server_events import ServerEvents

SHAKER = "shaker_1"


async def _shaker_runtime(gateway: DeviceGateway | None = None) -> ISystemRuntime:
    topology = Topology(locations={SHAKER: UniversalMockDevice(SHAKER)}, transporters=[])
    build = await build_system(
        "one_recorder", topology, package_stores(),
        event_bus=EventBus(), ops_history_store=memory_ops_store(),
    )
    runtime = await memory_runtime(
        build.system, event_bus=build.event_bus, gateway_registry=NullGatewayRegistry(), gateway=gateway,
    )
    await runtime.start()
    return runtime


@pytest.mark.timeout(30)
async def test_a_command_with_no_typed_request_still_leaves_a_record() -> None:
    """A verb outside the liquid-handler table leaves the trail its action does.

    The operator reader only knew liquid-handler commands, so a shake, a seal
    or an incubation an operator ran by hand was absent from the history that a
    workflow's identical action fills in. Nothing about a shaker makes it less
    worth recording than a pipette.
    """
    runtime = await _shaker_runtime()
    try:
        await record_operator_command(
            runtime.system, SHAKER, "shake", {"speed": 500.0, "duration": 30.0},
            interfaces=frozenset({"IShaker"}),
        )

        hits = await runtime.system.ops_history.store.search(OpsHistorySearchQuery())
        shakes = [
            op for _execution_id, record in hits for op in record.operations
            if op.operation is DeviceOperation.SHAKE
        ]
        assert len(shakes) == 1, f"expected one shake, found {len(shakes)}"
        assert shakes[0].device_name == SHAKER
        assert shakes[0].source is TrackingSource.OPERATOR
        assert shakes[0].thread_id == SYSTEM_ID
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(30)
async def test_a_command_another_device_shares_a_name_with_is_read_as_its_own() -> None:
    """A stacker's dispense releases a plate; it is not a liquid handler's dispense."""
    runtime = await _shaker_runtime()
    try:
        await record_operator_command(runtime.system, SHAKER, "dispense", {}, interfaces=frozenset({"IStorage"}))

        hits = await runtime.system.ops_history.store.search(OpsHistorySearchQuery())
        assert [op.details for _id, record in hits for op in record.operations] == [
            GenericOperationDetails(command="dispense", args_repr="{}"),
        ]
    finally:
        await runtime.shutdown()


async def _commands_on_record(runtime: ISystemRuntime) -> list[tuple[str, TrackingSource]]:
    hits = await runtime.system.ops_history.store.search(OpsHistorySearchQuery())
    return [
        (op.details.command, op.source)
        for _id, record in hits for op in record.operations
        if op.operation is DeviceOperation.COMMAND and isinstance(op.details, GenericOperationDetails)
    ]


@pytest.mark.timeout(30)
async def test_an_operators_initialize_connect_and_disconnect_are_recorded() -> None:
    gateway = build_device_gateway(ServerEvents(), None)
    connected = DeviceSnapshot(
        type="shaker", name=SHAKER, interfaces=[], capabilities=[], provides_state=False, methods={},
        site="test", lab="test", workcell=None, status="ready", last_seen=datetime.now(timezone.utc),
    )
    with (
        patch.object(type(gateway.tracker), "get_device", AsyncMock(return_value=connected)),
        patch.object(type(gateway.controller), "execute_command", AsyncMock(return_value=None)),
    ):
        await _lifecycle_verbs_on_record(gateway)


async def _lifecycle_verbs_on_record(gateway: DeviceGateway) -> None:
    runtime = await _shaker_runtime(gateway)
    runner = OperatorDeviceCommands(gateway, lambda: runtime)
    try:
        await InitializeDeviceOperation(runtime=runtime, devices=runner).run(InitializeDeviceRequest(device_id=SHAKER))
        await ConnectDeviceOperation(runtime=runtime, devices=runner).run(ConnectDeviceRequest(device_id=SHAKER))
        await DisconnectDeviceOperation(runtime=runtime, devices=runner).run(DisconnectDeviceRequest(device_id=SHAKER))

        assert await _commands_on_record(runtime) == [
            ("initialize", TrackingSource.OPERATOR),
            ("connect", TrackingSource.OPERATOR),
            ("disconnect", TrackingSource.OPERATOR),
        ]
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(30)
async def test_bringing_a_device_up_for_a_run_is_recorded() -> None:
    """Bring-up initializes each device, which homes an arm: a command like any other."""
    runtime = await _shaker_runtime()
    try:
        with mode_scope(WorkflowRunMode.SIM):
            assert await runtime.system.bring_up_for_a_run({}) == []

        assert await _commands_on_record(runtime) == [("initialize", TrackingSource.OBSERVED)]
    finally:
        await runtime.shutdown()


def test_a_tip_pick_up_record_keeps_the_channels_carrying_the_tips() -> None:
    request = PickUpTipsRequest(
        picks=[TipPick(tip_rack="rack_a", positions=["A1", "B1"])],
        use_channels=[2, 3],
    )

    [picked] = operations_for_request(request, _context(TrackingSource.OPERATOR))

    assert picked.affected_labware == ["rack_a"]
    assert isinstance(picked.details, TipPickUpDetails)
    assert picked.details.use_channels == [2, 3], (
        "a record that drops the channels says a tip was taken and not what is carrying it"
    )


def test_every_liquid_handler_command_is_read_or_declared_recorded_by_name() -> None:
    """The command table cannot grow a silent hole.

    A command in neither the reader nor ``RECORDED_BY_NAME`` could move tips or
    liquid and be saved as a record that says nothing about them. Losing the
    tip discard that way cost a head that read as loaded for the rest of a run.
    """
    commands = COMMANDS.commands_of("ILiquidHandler")
    assert commands, "the command table lists no liquid handler commands"
    unread = [
        name for name, command in commands.items()
        if name not in RECORDED_BY_NAME and not command_operations.reads(command.request_model)
    ]
    assert not unread, (
        f"{unread} have no reader; give each a branch in operations_for_request "
        f"or say in RECORDED_BY_NAME why its name is enough"
    )
    stale = sorted(set(RECORDED_BY_NAME) - set(commands))
    assert not stale, f"RECORDED_BY_NAME names commands the driver no longer takes: {stale}"


def test_every_liquid_handler_command_with_a_reader_is_read_on_the_workflow_route_too() -> None:
    """A verb the workflow route cannot build a request for is saved by name, and its tips or volumes are lost."""
    unbuilt = sorted(
        name for name, command in COMMANDS.commands_of("ILiquidHandler").items()
        if command_operations.reads(command.request_model) and name not in _REQUEST_BUILDERS
    )
    assert not unbuilt, f"{unbuilt} have a reader and no request builder in the liquid handler interpreter"


def test_a_command_that_changes_no_labware_is_still_recorded_whoever_sent_it() -> None:
    """Every command sent to a device is an operation in the history, home included."""
    home = GenericOperationDetails(command="home", args_repr="()")
    engine_lh = LiquidHandlerInterpreter().interpret("home", (), {}, None, "lh", [], [], "action-1", "thread-1")
    engine_other = DefaultInterpreter().interpret("home", (), {}, None, "arm", [], [], "action-1", "thread-1")
    operator = operations_for_command("home", "()", _context(TrackingSource.OPERATOR))

    for records in (engine_lh, engine_other, operator):
        assert [(op.operation, op.details) for op in records] == [(DeviceOperation.COMMAND, home)]


def test_a_liquid_handler_request_that_changes_no_labware_is_recorded_by_name() -> None:
    records = operations_for("get_deck_state", GetDeckStateRequest(), "{}", _context(TrackingSource.OPERATOR))

    assert [(op.operation, op.details) for op in records] == [
        (DeviceOperation.COMMAND, GenericOperationDetails(command="get_deck_state", args_repr="{}")),
    ]


def _context(source: TrackingSource) -> RecordContext:
    return RecordContext(
        device_name="lh",
        action_id="action-1",
        thread_id="thread-1",
        source=source,
        resolve_labware_id={"rack_a": "rack-a-id"}.get,
    )


def test_a_request_nothing_can_read_is_an_error_not_an_empty_record() -> None:
    class _NewVerbRequest(BaseModel):
        positions: list[str]

    with pytest.raises(command_operations.UnreadCommandError):
        operations_for_request(_NewVerbRequest(positions=["A1"]), _context(TrackingSource.OPERATOR))
