"""ConnectionEventBus -- in-process pub/sub for device connection state.

Per-device `device.connected`, `device.disconnected` and `device.reported`
signals let the runtime registry, the disconnect-policy machinery, and any
other in-process subscriber react to device-bridge state changes without
polling. Independent of the orca-core runtime `SystemEventBus` because these
events are not execution-scoped: a device bridge may connect, disconnect or
report between executions, before the runtime is built, or while it's idle.

The bus is a thin async-callback registry. Subscribers register an async
callback; the websocket layer calls `emit_connected` / `emit_disconnected` /
`emit_reported` at the right moments. Listeners are awaited sequentially so an
unhandled exception in one listener doesn't lose subsequent listeners (each
call is guarded). Listeners are expected to be fast: log the event, update an
in-memory tracker, fire a downstream coroutine, etc.
"""

import asyncio
import logging
from typing import Awaitable, Callable, List, Protocol

from cheshire_drivers.gateway_protocol import DeviceConnectInfo, DeviceStatusInfo

from orca.gateway.registry.connection_tracker import IDeviceConnectionTracker

logger = logging.getLogger(__name__)


ConnectedListener = Callable[[DeviceConnectInfo, str], Awaitable[None]]
DisconnectedListener = Callable[[str], Awaitable[None]]
ReportedListener = Callable[[str, DeviceStatusInfo], Awaitable[None]]


class IConnectionEvents(Protocol):
    """Per-device connect, disconnect and report signals from the device bridges."""

    def subscribe_connected(self, listener: ConnectedListener) -> None: ...

    def subscribe_disconnected(self, listener: DisconnectedListener) -> None: ...

    def subscribe_reported(self, listener: ReportedListener) -> None: ...

    def unsubscribe_connected(self, listener: ConnectedListener) -> None: ...

    def unsubscribe_disconnected(self, listener: DisconnectedListener) -> None: ...

    def unsubscribe_reported(self, listener: ReportedListener) -> None: ...

    async def emit_connected(
        self, device: DeviceConnectInfo, client_id: str,
    ) -> None: ...

    async def emit_disconnected(self, device_id: str) -> None: ...

    async def emit_reported(
        self, device_name: str, reported: DeviceStatusInfo,
    ) -> None: ...


class ConnectionEventBus:
    """Async-callback registry for per-device connect / disconnect signals.

    Subscriptions are unbounded: there's no automatic cleanup. Each
    subscriber unsubscribes when it shuts down.
    """

    def __init__(self) -> None:
        self._connected: List[ConnectedListener] = []
        self._disconnected: List[DisconnectedListener] = []
        self._reported: List[ReportedListener] = []

    def subscribe_connected(self, listener: ConnectedListener) -> None:
        self._connected.append(listener)

    def subscribe_disconnected(self, listener: DisconnectedListener) -> None:
        self._disconnected.append(listener)

    def subscribe_reported(self, listener: ReportedListener) -> None:
        self._reported.append(listener)

    def unsubscribe_connected(self, listener: ConnectedListener) -> None:
        try:
            self._connected.remove(listener)
        except ValueError:
            pass

    def unsubscribe_disconnected(self, listener: DisconnectedListener) -> None:
        try:
            self._disconnected.remove(listener)
        except ValueError:
            pass

    def unsubscribe_reported(self, listener: ReportedListener) -> None:
        try:
            self._reported.remove(listener)
        except ValueError:
            pass

    async def emit_connected(
        self, device: DeviceConnectInfo, client_id: str,
    ) -> None:
        """Fire `device.connected` for one device. Listener errors are logged."""
        for listener in list(self._connected):
            try:
                await listener(device, client_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "device.connected listener failed for device %r (client %r)",
                    device.name,
                    client_id,
                )

    async def emit_disconnected(self, device_id: str) -> None:
        """Fire `device.disconnected` for one device id. Listener errors are logged."""
        for listener in list(self._disconnected):
            try:
                await listener(device_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "device.disconnected listener failed for device %r",
                    device_id,
                )

    async def emit_reported(
        self, device_name: str, reported: DeviceStatusInfo,
    ) -> None:
        """Fire `device.reported` for one device. Listener errors are logged.

        Carries the whole report rather than a collapsed flag: the device
        bridge reports one link per run mode and `DeviceStatusInfo` documents
        why no single naive collapse serves every reader.
        """
        for listener in list(self._reported):
            try:
                await listener(device_name, reported)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "device.reported listener failed for device %r", device_name,
                )


async def take_status_report(
    tracker: IDeviceConnectionTracker, events: IConnectionEvents, client_id: str, devices: dict[str, DeviceStatusInfo],
) -> None:
    """Record what an orca-client sees on each device it owns, and tell the listeners."""
    for name, reported in devices.items():
        if await tracker.apply_agent_report(client_id, name, reported):
            await events.emit_reported(name, reported)
