"""A restarted device bridge stops orca claiming its devices are brought up.

A device bridge restart is routine: the on-prem process is upgraded, the box
reboots, the socket drops. Every driver object the device bridge held dies with
it, so nothing behind the wire is initialized any more, however recently orca
watched a bring-up succeed.

orca's remote drivers cache that flag so a synchronous `is_initialized` read
costs no wire round trip, and nothing used to clear it. `Transporter.
ensure_initialized` therefore no-opped after a device bridge restart and the
next pick drove an arm that had never been brought up and, per
`Transporter.initialize`, never homed since power-on. The same cache backs every
other remote driver, and the system's own memory of which worlds it brought up
had the same hole.
"""

from dataclasses import dataclass
from typing import Optional, cast

import pytest

from cheshire_drivers.gateway_protocol import (
    DeviceConnectInfo,
    DeviceLinkInfo,
    DeviceStatusInfo,
)
from pydantic import JsonValue

from orca.gateway.controller.command_kind import CommandKind
from orca.devices.shaker import Shaker
from orca.events.event_bus import EventBus
from orca.gateway.controller.controller import DeviceController
from orca.gateway.remote_device_factory import RemoteDeviceFactory
from orca.gateway.websocket.connection_events import connection_events
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap

from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    seeded,
    wire_system_map,
)

pytestmark = pytest.mark.asyncio

ARM = "robot1"
SHAKER = "shaker1"


@dataclass(frozen=True)
class _RecordedCommand:
    device_id: str
    command: str


class _RecordingController:
    """Stands in for the wire: records what orca dispatched at the device
    bridge."""

    def __init__(self) -> None:
        self.commands: list[_RecordedCommand] = []

    async def execute_command(
        self,
        device_id: str,
        command: str,
        params: Optional[dict[str, JsonValue]] = None,
        timeout_seconds: Optional[float] = None,
        effective_mode: WorkflowRunMode = WorkflowRunMode.LIVE,
        resend_on_reconnect: bool = True,
        kind: CommandKind = CommandKind.ACTUATION,
        execution_id: Optional[str] = None,
    ) -> JsonValue:
        self.commands.append(_RecordedCommand(device_id, command))
        return {}

    def count(self, device_id: str, command: str) -> int:
        return sum(
            1 for recorded in self.commands
            if recorded.device_id == device_id and recorded.command == command
        )

    def sequence(self, device_id: str) -> list[str]:
        return [
            recorded.command for recorded in self.commands
            if recorded.device_id == device_id
        ]


def _connect_info(name: str, device_type: str) -> DeviceConnectInfo:
    return DeviceConnectInfo(
        name=name, type=device_type, interfaces=frozenset(),
    )


def _agent_report(*, is_initialized: bool) -> DeviceStatusInfo:
    return DeviceStatusInfo(
        status="ready",
        links={
            "LIVE": DeviceLinkInfo(
                is_connected=True, is_initialized=is_initialized,
            ),
        },
    )


async def _runtime_over_an_agent() -> tuple[
    SystemRuntime, Transporter, _RecordingController,
]:
    """A shaker and an arm, both reached through one device bridge."""
    controller = _RecordingController()
    factory = RemoteDeviceFactory(
        controller=cast(DeviceController, controller),
        mode_resolver=lambda _name: WorkflowRunMode.LIVE,
    )
    with use_device_factory(factory):
        shaker = Shaker(SHAKER)
        transporter = create_test_transporter(ARM, ["pad1", SHAKER])

    registry = ResourceRegistry()
    registry.add_resource(shaker)
    registry.add_resource(transporter)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={SHAKER: shaker}, pads=["pad1"])
    builder = SdkToSystemBuilder(
        name="agent_restart_sys", description="",
        labwares=[create_test_plate_template("plate_96")],
        resources_registry=registry, system_map=system_map,
        workflows=[], event_bus=EventBus(),
    )
    await builder.bind_labwares()
    runtime = SystemRuntime(builder.get_system(), event_bus=EventBus())
    await runtime.start()
    return runtime, transporter, controller


async def _reconnect(name: str, device_type: str, runtime: SystemRuntime) -> None:
    await connection_events.emit_connected(_connect_info(name, device_type), "agent-1")
    await runtime.flush_deck_reseeds()


class TestAnArmIsBroughtUpAgainAfterItsAgentRestarts:
    async def test_a_reconnect_makes_ensure_initialized_bring_the_arm_up(self) -> None:
        """The bench hazard, as a test: the arm is sent a move having never
        been brought up because a cache outlived the session that earned it."""
        runtime, transporter, controller = await _runtime_over_an_agent()
        try:
            with seeded(WorkflowRunMode.LIVE):
                await transporter.ensure_initialized()
                assert controller.count(ARM, "initialize") == 1

                await _reconnect(ARM, "transporter", runtime)
                await transporter.ensure_initialized()

            assert controller.count(ARM, "initialize") == 2, (
                "after its device bridge reconnected the arm holds no bring-up, so the "
                "next pick must initialize it; observed "
                f"{controller.count(ARM, 'initialize')} initialize dispatches"
            )
        finally:
            await runtime.shutdown()
            connection_events.clear()

    async def test_the_link_is_reopened_before_the_arm_is_brought_up(self) -> None:
        """A rebuilt device bridge holds a closed link too, so a bring-up that
        skips `connect` initializes a driver that never reached the
        instrument."""
        runtime, transporter, controller = await _runtime_over_an_agent()
        try:
            with seeded(WorkflowRunMode.LIVE):
                await transporter.ensure_initialized()

                await _reconnect(ARM, "transporter", runtime)
                await transporter.ensure_initialized()

            assert controller.sequence(ARM) == [
                "connect", "initialize", "connect", "initialize",
            ], (
                "the second bring-up must reopen the link first; observed "
                f"{controller.sequence(ARM)}"
            )
        finally:
            await runtime.shutdown()
            connection_events.clear()

    async def test_an_agent_report_of_not_brought_up_ends_the_claim(self) -> None:
        """A restarted device bridge may not reconnect through this runtime at
        all, but it reports; the party holding the driver objects wins the
        argument."""
        runtime, transporter, controller = await _runtime_over_an_agent()
        try:
            with seeded(WorkflowRunMode.LIVE):
                await transporter.ensure_initialized()

                await connection_events.emit_reported(
                    ARM, _agent_report(is_initialized=False),
                )
                await transporter.ensure_initialized()

            assert controller.count(ARM, "initialize") == 2, (
                "the device bridge said the arm is not brought up, so the next pick "
                "must bring it up; observed "
                f"{controller.count(ARM, 'initialize')} initialize dispatches"
            )
        finally:
            await runtime.shutdown()
            connection_events.clear()

    async def test_a_report_that_the_arm_is_up_costs_no_bring_up(self) -> None:
        """The negative control: healthy reports arrive with every heartbeat,
        and re-initializing on each one would reset the arm's tracking all run."""
        runtime, transporter, controller = await _runtime_over_an_agent()
        try:
            with seeded(WorkflowRunMode.LIVE):
                await transporter.ensure_initialized()

                await connection_events.emit_reported(
                    ARM, _agent_report(is_initialized=True),
                )
                await transporter.ensure_initialized()

            assert controller.count(ARM, "initialize") == 1, (
                "a report agreeing the arm is up must leave it alone; observed "
                f"{controller.count(ARM, 'initialize')} initialize dispatches"
            )
        finally:
            await runtime.shutdown()
            connection_events.clear()

    async def test_a_shut_down_runtime_stops_answering_for_its_drivers(self) -> None:
        """A hosted deployment rebuilds SystemRuntime in place; a listener left behind would
        keep invalidating drivers a live successor now owns."""
        runtime, transporter, controller = await _runtime_over_an_agent()
        try:
            with seeded(WorkflowRunMode.LIVE):
                await transporter.ensure_initialized()
            await runtime.shutdown()

            await connection_events.emit_reported(
                ARM, _agent_report(is_initialized=False),
            )
            with seeded(WorkflowRunMode.LIVE):
                await transporter.ensure_initialized()

            assert controller.count(ARM, "initialize") == 1, (
                "a dead runtime must not touch driver state; observed "
                f"{controller.count(ARM, 'initialize')} initialize dispatches"
            )
        finally:
            connection_events.clear()


class TestTheWholeRemoteDriverFamilyForgets:
    async def test_a_reconnected_device_is_brought_up_by_the_next_walk(self) -> None:
        """Nothing re-checks a shaker before each command the way a pick
        re-checks the arm, so the bring-up walk is where its recovery lands.
        Both orca's memories have to let go: the driver's cached flag and the
        system's record of which worlds it already brought up."""
        runtime, _transporter, controller = await _runtime_over_an_agent()
        try:
            with seeded(WorkflowRunMode.LIVE):
                await runtime.system.ensure_runtime_initialized()
                assert controller.count(SHAKER, "initialize") == 1

                await _reconnect(SHAKER, "shaker", runtime)
                await runtime.system.ensure_runtime_initialized()

            assert controller.count(SHAKER, "initialize") == 2, (
                "a device whose device bridge restarted must come up again on the next "
                f"walk; observed {controller.count(SHAKER, 'initialize')} "
                "initialize dispatches"
            )
        finally:
            await runtime.shutdown()
            connection_events.clear()

    async def test_only_the_reconnected_device_comes_up_again(self) -> None:
        """Bringing a device up takes its session and resets what it was
        tracking, so one device bridge's return must not sweep the whole
        workcell."""
        runtime, _transporter, controller = await _runtime_over_an_agent()
        try:
            with seeded(WorkflowRunMode.LIVE):
                await runtime.system.ensure_runtime_initialized()

                await _reconnect(SHAKER, "shaker", runtime)
                await runtime.system.ensure_runtime_initialized()

            assert controller.count(ARM, "initialize") == 1, (
                "the arm's device bridge never went away, so its bring-up stands; "
                f"observed {controller.count(ARM, 'initialize')} initialize "
                "dispatches"
            )
        finally:
            await runtime.shutdown()
            connection_events.clear()
