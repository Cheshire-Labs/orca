"""A connected device's card and gateway row carry what its orca-client reported; a device no client holds has neither."""

import pytest
from cheshire_drivers.gateway_protocol import DeviceConnectInfo

from orca.gateway.connection_source import DeviceConnectionSource
from orca.gateway.registry.connection_tracker import DeviceConnectionTracker

pytestmark = pytest.mark.asyncio


async def _tracker_with_a_shaker() -> DeviceConnectionTracker:
    tracker = DeviceConnectionTracker()
    await tracker.register_client(
        client_id="client_1", site="site_1", lab="lab_1", workcell=None,
        devices=[DeviceConnectInfo(name="shaker_1", type="Shaker", interfaces=frozenset({"IShaker"}))],
    )
    return tracker


async def test_a_connected_devices_card_and_gateway_row_carry_its_heartbeat_time_kind_and_status() -> None:
    tracker = await _tracker_with_a_shaker()
    source = DeviceConnectionSource(tracker)
    snapshot = await tracker.get_device("shaker_1")
    assert snapshot is not None

    card = await source.get_connection_card("shaker_1")
    row = await source.get_gateway_status("shaker_1")

    assert card is not None and row is not None
    assert (card.last_heartbeat, card.advertised_kind, card.advertised_interfaces, card.client_id) == (
        snapshot.last_seen, "Shaker", frozenset({"IShaker"}), "client_1",
    )
    assert (row.last_heartbeat, row.driver_class_observed, row.interfaces, row.status) == (
        snapshot.last_seen, "Shaker", ("IShaker",), "ready",
    )
    assert [c.name for c in await source.list_connection_cards()] == ["shaker_1"]
    assert [r.name for r in await source.list_connected()] == ["shaker_1"]


async def test_a_device_whose_client_dropped_has_no_card_and_no_gateway_row() -> None:
    tracker = await _tracker_with_a_shaker()
    source = DeviceConnectionSource(tracker)

    await tracker.unregister_client("client_1")

    assert await source.get_connection_card("shaker_1") is None
    assert await source.get_gateway_status("shaker_1") is None
    assert await source.list_connection_cards() == []
    assert await source.list_connected() == []
