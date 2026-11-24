"""`GET /devices` is the only device list, transporters included."""

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from orca.gateway.connection_source import _connection_card_from_snapshot
from orca.gateway.registry.snapshot import DeviceSnapshot

from orca.devices.deck_gripper_transporter import DeckGripperTransporter
from orca.devices.devices import LiquidHandler
from orca.devices.shaker import Shaker
from orca.resource_models.labware import LabwareInstance
from orca.runtime.system_runtime import SystemRuntime


async def test_the_device_list_carries_the_engine_state_of_devices_and_transporters(
    client: AsyncClient,
) -> None:
    resp = await client.get("/api/devices")
    assert resp.status_code == 200
    by_name = {e["name"]: e for e in resp.json()["devices"]}

    shaker = by_name["shaker1"]["engine"]
    assert shaker["type_name"]
    assert shaker["is_busy"] is False
    assert shaker["position_ids"] == ["shaker1"]
    assert shaker["held_labware_id"] is None

    arm = by_name["robot1"]["engine"]
    assert arm["is_busy"] is False
    assert arm["position_ids"] == []
    assert arm["loaded_labware_ids"] == []
    assert arm["held_labware_id"] is None


async def test_the_old_device_list_is_gone(client: AsyncClient) -> None:
    resp = await client.get("/api/catalog/devices")
    assert resp.status_code == 404



async def test_the_device_list_says_what_a_transporter_holds(
    client: AsyncClient, runtime: SystemRuntime,
) -> None:
    [arm] = [t for t in runtime.system.transporters if t.name == "robot1"]
    plate = LabwareInstance("held_plate", "96_well")
    arm.gripper_location.initialize_labware(plate)

    resp = await client.get("/api/devices")

    by_name = {e["name"]: e for e in resp.json()["devices"]}
    assert by_name["robot1"]["engine"]["held_labware_id"] == plate.id


def test_a_device_with_no_mover_of_its_own_holds_nothing() -> None:
    assert Shaker("shaker_x").held_labware_id() is None


def test_a_liquid_handler_holds_what_its_deck_gripper_holds() -> None:
    lh = LiquidHandler("lh_x")
    gripper = DeckGripperTransporter(lh)
    lh.set_gripper(gripper)
    plate = LabwareInstance("gripped", "96_well")
    gripper.gripper_location.initialize_labware(plate)

    assert lh.held_labware_id() == plate.id


@pytest.mark.parametrize(("status", "busy"), [("busy", True), ("ready", False)])
def test_the_connection_card_says_when_the_device_bridge_reports_a_command_running(status: str, busy: bool) -> None:
    snapshot = DeviceSnapshot(
        type="liquid_handler", name="flex_1", interfaces=["ILiquidHandlerDriver"],
        capabilities=[], provides_state=False, methods={}, site="test", lab="test",
        workcell=None, status=status, last_seen=datetime.now(timezone.utc),
    )

    assert _connection_card_from_snapshot(snapshot, "client-1").device_is_busy is busy
