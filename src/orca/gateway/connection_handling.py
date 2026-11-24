"""What every host of the device gateway runs: the stale-connection sweep, the disconnect and reconnect hooks,
the device records, and a DEVICE event for each connect and disconnect."""

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from cheshire_drivers.gateway_protocol import DeviceConnectInfo

from orca.events.execution_context import DeviceConnectionContext
from orca.events.runtime_event import RuntimeEvent
from orca.gateway.device_gateway import ServerDeviceGateway
from orca.gateway.websocket.health_monitor import WireHealthMonitor
from orca.events.emitter import IEventEmitter
from orca.state.ops_store import SYSTEM_ID


def _connection_event(device_name: str, status: str, client_id: str | None) -> RuntimeEvent:
    return RuntimeEvent(
        event_name=f"DEVICE.{device_name}.{status}",
        execution_id=SYSTEM_ID,
        timestamp=time.time(),
        entity_type="DEVICE",
        entity_id=device_name,
        status=status,
        context=DeviceConnectionContext(device_name=device_name, client_id=client_id),
    )


@asynccontextmanager
async def device_connection_handling(
    gateway: ServerDeviceGateway, events: IEventEmitter,
) -> AsyncIterator[WireHealthMonitor]:
    """Sweep dead connections, handle disconnects and reconnects, and send each one to `events`, for the life of the block."""

    async def fail_the_command_in_flight(device: DeviceConnectInfo, _client_id: str) -> None:
        await gateway.controller.on_device_reconnected(device.name)

    async def announce_connected(device: DeviceConnectInfo, client_id: str) -> None:
        events.emit(_connection_event(device.name, "CONNECTED", client_id))

    async def announce_disconnected(device_name: str) -> None:
        events.emit(_connection_event(device_name, "DISCONNECTED", None))

    monitor = WireHealthMonitor(gateway.sockets)
    monitor.start()
    gateway.events.subscribe_disconnected(gateway.controller.on_device_disconnected)
    gateway.events.subscribe_connected(fail_the_command_in_flight)
    gateway.events.subscribe_connected(announce_connected)
    gateway.events.subscribe_disconnected(announce_disconnected)
    try:
        yield monitor
    finally:
        gateway.events.unsubscribe_disconnected(announce_disconnected)
        gateway.events.unsubscribe_connected(announce_connected)
        gateway.events.unsubscribe_connected(fail_the_command_in_flight)
        gateway.events.unsubscribe_disconnected(gateway.controller.on_device_disconnected)
        await monitor.stop()
