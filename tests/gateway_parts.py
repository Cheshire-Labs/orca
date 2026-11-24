"""A fresh device gateway per test, with its concrete parts in reach."""

from dataclasses import dataclass, fields
from unittest.mock import MagicMock

from cheshire_drivers.gateway_protocol import AssignmentResult, DeviceAssignment, DeviceConnectInfo, MessageEnvelope
from starlette.testclient import WebSocketTestSession

from orca.gateway.controller.command_clock import DeviceCommandClock
from orca.gateway.controller.controller import DeviceController, IDeviceController
from orca.gateway.controller.disconnect_grace import DisconnectGrace
from orca.gateway.device_gateway import DeviceGateway, ServerDeviceGateway
from orca.gateway.registry.connection_tracker import (
    DeviceConnectionTracker,
    IDeviceConnectionTracker,
)
from orca.gateway.websocket.assignments import DeviceAssignments, IDeviceAssignments
from orca.gateway.websocket.connection_events import ConnectionEventBus, IConnectionEvents
from orca.gateway.websocket.manager import ConnectionManager, IConnectionManager
from orca.events.emitter import IEventEmitter
from orca.runtime.server_events import ServerEvents


@dataclass(frozen=True)
class GatewayParts:
    tracker: DeviceConnectionTracker
    connections: ConnectionManager
    events: ConnectionEventBus
    controller: DeviceController
    assignments: DeviceAssignments

    @property
    def gateway(self) -> ServerDeviceGateway:
        return ServerDeviceGateway(
            tracker=self.tracker,
            connections=self.connections,
            events=self.events,
            controller=self.controller,
            assignments=self.assignments,
            sockets=self.connections,
        )


def new_controller(
    tracker: IDeviceConnectionTracker | MagicMock,
    connections: IConnectionManager | MagicMock,
    *,
    disconnect_grace: DisconnectGrace | None = None,
    command_clock: DeviceCommandClock | None = None,
    events: IEventEmitter | None = None,
    default_sim_speedup: float | None = None,
) -> DeviceController:
    """A controller over the test's tracker and sockets, with the default grace and clock unless given."""
    return DeviceController(
        tracker=tracker,
        connections=connections,
        disconnect_grace=disconnect_grace if disconnect_grace is not None else DisconnectGrace(),
        command_clock=command_clock if command_clock is not None else DeviceCommandClock(),
        events=events if events is not None else ServerEvents(),
        default_sim_speedup=default_sim_speedup,
    )


def answer_assignment(ws: WebSocketTestSession, *built: DeviceConnectInfo) -> None:
    """Read the assignment the server sent after the hello and report `built` for it."""
    assignment = MessageEnvelope.model_validate_json(ws.receive_text()).unwrap()
    assert isinstance(assignment, DeviceAssignment)
    result = AssignmentResult(assignment_id=assignment.assignment_id, devices=list(built))
    ws.send_text(MessageEnvelope.wrap_assignment_result(result).model_dump_json())


def gateway_parts() -> GatewayParts:
    tracker = DeviceConnectionTracker()
    connections = ConnectionManager()
    return GatewayParts(
        tracker=tracker,
        connections=connections,
        events=ConnectionEventBus(),
        controller=new_controller(tracker, connections),
        assignments=DeviceAssignments(connections),
    )


def gateway_with(
    *,
    controller: IDeviceController | MagicMock | None = None,
    tracker: IDeviceConnectionTracker | MagicMock | None = None,
    connections: IConnectionManager | MagicMock | None = None,
    events: IConnectionEvents | MagicMock | None = None,
    assignments: IDeviceAssignments | MagicMock | None = None,
) -> ServerDeviceGateway:
    """A gateway whose named parts are the test's own; the rest are fresh and real."""
    parts = gateway_parts()
    sockets = connections if connections is not None else parts.connections
    return ServerDeviceGateway(
        tracker=tracker if tracker is not None else parts.tracker,
        connections=sockets,
        sockets=sockets,
        events=events if events is not None else parts.events,
        controller=controller if controller is not None else parts.controller,
        assignments=assignments if assignments is not None else parts.assignments,
    )


def served(gateway: DeviceGateway) -> ServerDeviceGateway:
    """`gateway` as a server holds it. A check run's gateway gets sockets of its own that no client has opened."""
    if isinstance(gateway, ServerDeviceGateway):
        return gateway
    return ServerDeviceGateway(
        **{f.name: getattr(gateway, f.name) for f in fields(DeviceGateway)}, sockets=ConnectionManager(),
    )
