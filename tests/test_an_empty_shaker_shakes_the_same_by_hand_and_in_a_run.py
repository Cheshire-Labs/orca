"""An empty shaker shaken by hand and in a run leaves the same records, over the real device path.

Both benches send through the forwarding driver, the device controller and an orca-client's executor. Only who
sent the command may differ: the record's source and bucket.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import Mock

import pytest

from orca.devices.shaker import Shaker
from orca.events.execution_context import DeviceCommandContext, MethodExecutionContext
from orca.gateway.device_gateway import DeviceGateway
from orca.gateway.gateway_backed_driver import GatewayBackedDriver
from orca.gateway.operator_commands import OperatorDeviceCommands
from orca.resource_models.plate_pad import PlatePad
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.sinks import CollectorSink
from orca.events.event_bus import EventBus
from orca.sdk.build import Topology
from orca.sdk.package import build_system
from orca.state.ops_store import OpsHistorySearchQuery
from orca.state.records import TrackingSource
from orca.workflow_models.actions.device_call_dispatcher import CommandRecorder, DeviceCallDispatcher
from orca.workflow_models.actions.executable_location_action import ExecutableLocationAction
from orca.workflow_models.device_handle import ActionRequest

from tests.memory_activity import memory_ops_store, memory_runtime
from tests.real_device_path import real_device_path
from tests.setup_package import package_stores

pytestmark = pytest.mark.asyncio

RUN_ID = "run_1"


class _Bench:
    def __init__(self, runtime: ISystemRuntime, seen: CollectorSink, gateway: DeviceGateway) -> None:
        self.runtime = runtime
        self.seen = seen
        self.gateway = gateway


@asynccontextmanager
async def _bench() -> AsyncIterator[_Bench]:
    seen = CollectorSink()
    async with real_device_path(package_stores()) as path:
        path.events.add_sink(seen)
        topology = Topology(locations={"shaker_1": Shaker("shaker_1"), "pad_1": PlatePad("pad_1")}, transporters=[])
        built = await build_system(
            "empty_shaker", topology, package_stores(), event_bus=EventBus(), ops_history_store=memory_ops_store(),
        )
        runtime = await memory_runtime(built.system, event_bus=built.event_bus)
        await runtime.start()
        try:
            yield _Bench(runtime, seen, path.gateway)
        finally:
            await runtime.shutdown(confirm=True)


async def _by_hand(bench: _Bench) -> None:
    runner = OperatorDeviceCommands(bench.gateway, lambda: bench.runtime)
    await runner.run_named("shaker_1", "shake", {"speed": 500.0, "duration": 1.0})


async def _in_a_run(bench: _Bench) -> None:
    system = bench.runtime.system
    device = system.get_device("shaker_1")
    action = Mock(id="action_1", command="shake", device=device, declares=None)
    action.build_template_to_instance_map.return_value = {}
    executable = ExecutableLocationAction(
        Mock(), action,
        MethodExecutionContext(
            execution_id=RUN_ID, workflow_name="shake", method_id="m1", method_name="method",
            thread_id="thread_1", thread_name="thread_1", participating_thread_ids=("thread_1",),
        ),
        tracking_context=system.tracking_context,
    )
    executable._wire_recorder()
    recorder: CommandRecorder = action.set_recorder.call_args[0][0]
    dispatcher = DeviceCallDispatcher(
        device=device, recorder=recorder, pipeline=device.engine_commands.pipeline,
        action_id="action_1", execution_id=RUN_ID,
    )
    await dispatcher.dispatch(
        ActionRequest(device_name="shaker_1", command="shake", args=(1.0, 500.0), kwargs={}), [], [], [],
    )


async def _what_it_left(bench: _Bench) -> tuple[list[tuple[str, str]], list[tuple[str, str, list[str]]], list[str]]:
    hits = await bench.runtime.system.ops_history.store.search(OpsHistorySearchQuery())
    ops = [
        (op.operation.value, op.device_name, op.affected_labware)
        for _execution, record in hits for op in record.operations
        if op.source in (TrackingSource.OPERATOR, TrackingSource.OBSERVED) and op.device_name == "shaker_1"
    ]
    commands = [
        (event.status or "", event.context.command)
        for event in bench.seen.events
        if event.event_name.startswith("COMMAND.shaker_1.") and isinstance(event.context, DeviceCommandContext)
    ]
    positions = sorted(
        f"{location.position_id}={location.labware.name if location.labware else None}"
        for location in bench.runtime.system.system_map.locations
    )
    return commands, ops, positions


@pytest.mark.timeout(60)
async def test_an_empty_shaker_shaken_by_hand_and_in_a_run_leaves_the_same_records() -> None:
    async with _bench() as by_hand, _bench() as by_run:
        await _by_hand(by_hand)
        await _in_a_run(by_run)

        hand = await _what_it_left(by_hand)
        run = await _what_it_left(by_run)
        # Each command reached the device through the controller, not an in-process simulator.
        assert isinstance(by_run.runtime.system.get_device("shaker_1").driver, GatewayBackedDriver)

    assert hand[0] == [("STARTED", "shake"), ("COMPLETED", "shake")]
    assert hand[1] == [("shake", "shaker_1", [])], "the record names no labware: nothing stood at the shaker"
    assert hand == run
