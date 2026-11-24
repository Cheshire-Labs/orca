"""Tests for the ConnectionEventBus."""

from typing import List, Tuple

import pytest

from cheshire_drivers.gateway_protocol import (
    DeviceConnectInfo,
    DeviceLinkInfo,
    DeviceStatusInfo,
)
from orca.gateway.websocket.connection_events import ConnectionEventBus


def _device(
    name: str = "shaker_1",
    type: str = "shaker",
    interfaces: frozenset[str] | None = None,
) -> DeviceConnectInfo:
    return DeviceConnectInfo(
        name=name,
        type=type,
        interfaces=interfaces if interfaces is not None else frozenset({"IShaker"}),
        capabilities=frozenset(),
        provides_state=False,
        methods={},
    )


@pytest.mark.asyncio
class TestConnectionEventBus:
    async def test_emit_connected_calls_every_listener(self) -> None:
        bus = ConnectionEventBus()
        called: List[Tuple[str, str]] = []

        async def listener_a(device: DeviceConnectInfo, client_id: str) -> None:
            called.append((f"a:{device.name}", client_id))

        async def listener_b(device: DeviceConnectInfo, client_id: str) -> None:
            called.append((f"b:{device.name}", client_id))

        bus.subscribe_connected(listener_a)
        bus.subscribe_connected(listener_b)

        await bus.emit_connected(_device("shaker_1"), "client_x")

        assert called == [("a:shaker_1", "client_x"), ("b:shaker_1", "client_x")]

    async def test_emit_disconnected_calls_every_listener(self) -> None:
        bus = ConnectionEventBus()
        called: List[str] = []

        async def listener(device_id: str) -> None:
            called.append(device_id)

        bus.subscribe_disconnected(listener)
        await bus.emit_disconnected("shaker_1")

        assert called == ["shaker_1"]

    async def test_emit_reported_calls_every_listener(self) -> None:
        """The runtime learns a device is not brought up from this event."""
        bus = ConnectionEventBus()
        called: List[Tuple[str, bool]] = []

        async def listener(device_name: str, reported: DeviceStatusInfo) -> None:
            called.append((device_name, reported.observed_link.is_initialized))

        bus.subscribe_reported(listener)
        await bus.emit_reported(
            "shaker_1",
            DeviceStatusInfo(
                status="ready",
                links={"LIVE": DeviceLinkInfo(
                    is_connected=True, is_initialized=False,
                )},
            ),
        )

        assert called == [("shaker_1", False)]

    async def test_listener_exception_does_not_block_others(self) -> None:
        """One listener raising must not skip subsequent listeners."""
        bus = ConnectionEventBus()
        called: List[str] = []

        async def boom(device_id: str) -> None:
            raise RuntimeError("listener failure")

        async def good(device_id: str) -> None:
            called.append(device_id)

        bus.subscribe_disconnected(boom)
        bus.subscribe_disconnected(good)

        await bus.emit_disconnected("shaker_1")

        assert called == ["shaker_1"]

    async def test_unsubscribe_removes_listener(self) -> None:
        bus = ConnectionEventBus()
        called: List[str] = []

        async def listener(device_id: str) -> None:
            called.append(device_id)

        bus.subscribe_disconnected(listener)
        bus.unsubscribe_disconnected(listener)
        await bus.emit_disconnected("shaker_1")

        assert called == []
