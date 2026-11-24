"""A client's status report changes only the devices that client owns."""

from datetime import datetime, timedelta, timezone

import pytest
from cheshire_drivers.gateway_protocol import DeviceConnectInfo, DeviceLinkInfo, DeviceStatusInfo

from orca.gateway.registry.connection_tracker import DeviceConnectionTracker
from orca.gateway.websocket.connection_events import ConnectionEventBus, take_status_report

_SHAKER = DeviceConnectInfo.model_validate({"type": "shaker", "name": "shaker_1", "interfaces": ["IShaker"]})
_NOT_BROUGHT_UP = DeviceStatusInfo(status="ready", links={"LIVE": DeviceLinkInfo(is_connected=False, is_initialized=False)})


@pytest.mark.asyncio
async def test_the_old_clients_report_for_a_device_that_moved_changes_nothing() -> None:
    tracker = DeviceConnectionTracker()
    events = ConnectionEventBus()
    heard: list[str] = []

    async def record(name: str, _reported: DeviceStatusInfo) -> None:
        heard.append(name)

    events.subscribe_reported(record)
    await tracker.register_client(client_id="old_pc", site="s", lab="l", workcell=None, devices=[_SHAKER])
    await tracker.register_client(client_id="new_pc", site="s", lab="l", workcell=None, devices=[_SHAKER])
    await take_status_report(
        tracker, events, "new_pc",
        {"shaker_1": DeviceStatusInfo(status="busy", links={"LIVE": DeviceLinkInfo(is_connected=True, is_initialized=True)})},
    )
    heard.clear()
    owned = tracker.peek_snapshot("shaker_1")
    assert owned is not None
    owned.last_seen = datetime.now(timezone.utc) - timedelta(seconds=5)
    before = owned.model_copy()

    await take_status_report(tracker, events, "old_pc", {"shaker_1": _NOT_BROUGHT_UP})

    after = tracker.peek_snapshot("shaker_1")
    assert after == before
    assert heard == []
