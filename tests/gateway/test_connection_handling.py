"""A gateway host's disconnect and reconnect hooks and its device records are on for the block and off after it."""


import pytest
from cheshire_drivers.gateway_protocol import DeviceConnectInfo

from orca.gateway.connection_handling import device_connection_handling
from tests.gateway_parts import gateway_parts
from orca.runtime.server_events import ServerEvents
from orca.runtime.sinks import CollectorSink


async def test_a_disconnect_reaches_the_controller_only_inside_the_block(monkeypatch: pytest.MonkeyPatch) -> None:
    parts = gateway_parts()
    seen: list[str] = []

    async def record(name: str) -> None:
        seen.append(name)

    monkeypatch.setattr(parts.controller, "on_device_disconnected", record)
    async with device_connection_handling(parts.gateway, ServerEvents()):
        await parts.events.emit_disconnected("shaker_1")
    await parts.events.emit_disconnected("shaker_2")

    assert seen == ["shaker_1"]


async def test_a_reconnect_reaches_the_controller_only_inside_the_block(monkeypatch: pytest.MonkeyPatch) -> None:
    parts = gateway_parts()
    seen: list[str] = []

    async def record(name: str) -> None:
        seen.append(name)

    monkeypatch.setattr(parts.controller, "on_device_reconnected", record)
    shaker = DeviceConnectInfo.model_validate({"type": "shaker", "name": "shaker_1", "interfaces": ["IShaker"]})
    async with device_connection_handling(parts.gateway, ServerEvents()):
        await parts.events.emit_connected(shaker, "lab_pc")
    await parts.events.emit_connected(shaker, "lab_pc")

    assert seen == ["shaker_1"]


async def test_each_connect_and_disconnect_is_sent_as_a_device_event() -> None:
    parts = gateway_parts()
    sink = CollectorSink()
    events = ServerEvents()
    events.add_sink(sink)
    shaker = DeviceConnectInfo.model_validate({"type": "shaker", "name": "shaker_1", "interfaces": ["IShaker"]})

    async with device_connection_handling(parts.gateway, events):
        await parts.events.emit_connected(shaker, "lab_pc")
        await parts.events.emit_disconnected("shaker_1")

    assert [e.event_name for e in sink.events] == ["DEVICE.shaker_1.CONNECTED", "DEVICE.shaker_1.DISCONNECTED"]
