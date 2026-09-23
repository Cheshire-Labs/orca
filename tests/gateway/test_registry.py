"""Tests for device registry."""

import pytest
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pydantic

from cheshire_drivers.gateway_protocol import (
    DeviceConnectInfo,
    DeviceLinkInfo,
    DeviceStatusInfo,
)
from orca.gateway.registry import DeviceConnectionTracker
from orca.gateway.registry.connection_tracker import DeviceNameConflictError

# Test UUIDs for multi-tenant testing
TEST_DEPLOYMENT_ID_1 = UUID("11111111-1111-1111-1111-111111111111")
TEST_DEPLOYMENT_ID_2 = UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture
def registry():
    """Create a fresh device registry for each test."""
    return DeviceConnectionTracker()


@pytest.mark.asyncio
class TestDeviceConnectionTracker:
    """Tests for DeviceConnectionTracker class."""

    async def test_register_unregister_state_machine(self, registry):
        """Pin the full register/unregister transition contract.

        Walks register-first -> register-second -> unregister-first ->
        unregister-second and asserts the client + device counts at each
        step. Replaces the prior setup-trace test that only confirmed
        the initial register call moved the counter to 1.
        """
        client1_devices = [
            DeviceConnectInfo.model_validate({"type": "shaker", "name": "shaker_1", "interfaces": ["IShaker"]}),
            DeviceConnectInfo.model_validate({"type": "centrifuge", "name": "centrifuge_1", "interfaces": ["ICentrifuge"]}),
        ]
        client2_devices = [
            DeviceConnectInfo.model_validate({"type": "shaker", "name": "shaker_2", "interfaces": ["IShaker"]}),
        ]

        # Initial state: no clients, no devices.
        assert registry.get_active_client_count() == 0
        assert registry.get_active_device_count() == 0

        # Register first client -> 1 client, 2 devices.
        await registry.register_client(
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
            devices=client1_devices,
        )
        assert registry.get_active_client_count() == 1
        assert registry.get_active_device_count() == 2

        # Pin the device snapshot's identity round-trip: site / lab / type
        # must flow from register_client() through to the public snapshot.
        # Also pin that client_id is intentionally NOT exposed on the
        # public snapshot (callers needing it use get_client_for_device).
        shaker_snap = await registry.get_device("shaker_1")
        assert shaker_snap is not None
        assert shaker_snap.site == "boston"
        assert shaker_snap.lab == "molbio"
        assert shaker_snap.type == "shaker"
        assert not hasattr(shaker_snap, "client_id")

        # Register second client -> 2 clients, 3 devices.
        await registry.register_client(
            client_id="lab2-client",
            site="cambridge",
            lab="cellculture",
            workcell=None,
            devices=client2_devices,
        )
        assert registry.get_active_client_count() == 2
        assert registry.get_active_device_count() == 3

        # Unregister first client -> 1 client, 1 device (client 2's).
        await registry.unregister_client("lab1-client")
        assert registry.get_active_client_count() == 1
        assert registry.get_active_device_count() == 1
        assert await registry.get_device("shaker_1") is None
        assert await registry.get_device("centrifuge_1") is None
        remaining = await registry.get_device("shaker_2")
        assert remaining is not None

        # Unregister second client -> empty again.
        await registry.unregister_client("lab2-client")
        assert registry.get_active_client_count() == 0
        assert registry.get_active_device_count() == 0
        assert await registry.get_device("shaker_2") is None

    async def test_a_second_client_cannot_claim_a_device_another_one_holds(self, registry):
        """One instrument, one connection. Two device bridges advertising the
        same device name is a misconfiguration, not a handover: both sockets
        stay open, both believe they own it, and every later command silently
        routes to whichever registered last. Refuse the newcomer and leave the
        incumbent in place."""
        held = [DeviceConnectInfo.model_validate(
            {"type": "transporter", "name": "pf400_1", "interfaces": ["ITransporter"]}
        )]

        await registry.register_client(
            client_id="boston-molbio-client", site="boston", lab="molbio",
            workcell=None, devices=held,
        )

        with pytest.raises(DeviceNameConflictError) as caught:
            await registry.register_client(
                client_id="cambridge-cellculture-client", site="cambridge",
                lab="cellculture", workcell=None, devices=held,
            )

        assert "pf400_1" in str(caught.value)
        assert "boston-molbio-client" in str(caught.value), "name the incumbent"

        # The incumbent still owns it, and the newcomer left no trace.
        assert await registry.get_client_for_device("pf400_1") == "boston-molbio-client"
        assert registry.get_active_client_count() == 1

    async def test_the_same_client_reconnecting_keeps_its_own_devices(self, registry):
        """A reconnect re-advertises the same names under the same client id.
        That is the normal path and must not be mistaken for a second claim."""
        devices = [DeviceConnectInfo.model_validate(
            {"type": "shaker", "name": "shaker_1", "interfaces": ["IShaker"]}
        )]
        for _ in range(2):
            await registry.register_client(
                client_id="boston-molbio-client", site="boston", lab="molbio",
                workcell=None, devices=devices,
            )

        assert registry.get_active_device_count() == 1
        assert await registry.get_client_for_device("shaker_1") == "boston-molbio-client"

    async def test_a_device_is_taken_over_once_its_owner_goes_quiet(self, registry):
        """A client whose heartbeat has lapsed is gone whether or not its socket
        was reaped, so a replacement device bridge must be able to pick the
        device up. Otherwise a network blip strands the instrument until someone
        restarts the server."""
        devices = [DeviceConnectInfo.model_validate(
            {"type": "shaker", "name": "shaker_1", "interfaces": ["IShaker"]}
        )]
        await registry.register_client(
            client_id="boston-molbio-client", site="boston", lab="molbio",
            workcell=None, devices=devices,
        )
        # Simulate a lapsed heartbeat: the tracker judges liveness off last_seen.
        stale = datetime.now(timezone.utc) - timedelta(seconds=90)
        registry._devices["shaker_1"].snapshot.last_seen = stale

        await registry.register_client(
            client_id="cambridge-cellculture-client", site="cambridge",
            lab="cellculture", workcell=None, devices=devices,
        )

        assert await registry.get_client_for_device("shaker_1") == "cambridge-cellculture-client"

    async def test_unregister_client(self, registry):
        """Test unregistering a client removes its devices."""
        devices = [
            DeviceConnectInfo.model_validate({"type": "shaker", "name": "shaker_1", "interfaces": ["IShaker"]}),
        ]

        await registry.register_client(
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
            devices=devices,
        )

        # Verify registered
        assert registry.get_active_device_count() == 1

        # Unregister
        await registry.unregister_client("lab1-client")

        # Verify removed
        assert registry.get_active_client_count() == 0
        assert registry.get_active_device_count() == 0

        device = await registry.get_device("shaker_1")
        assert device is None

    async def test_get_client_for_device(self, registry):
        """Test finding which client controls a device."""
        devices = [
            DeviceConnectInfo.model_validate({"type": "shaker", "name": "shaker_1", "interfaces": ["IShaker"]}),
        ]

        await registry.register_client(
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
            devices=devices,
        )

        client_id = await registry.get_client_for_device("shaker_1")
        assert client_id == "lab1-client"

        # Non-existent device
        client_id = await registry.get_client_for_device("nonexistent")
        assert client_id is None

    async def test_peek_interfaces(self, registry):
        """Sync read of a device's advertised interface set, used by the
        device factory to pick a liquid handler's per-instance profile.
        """
        await registry.register_client(
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
            devices=[
                DeviceConnectInfo.model_validate({
                    "type": "liquid_handler",
                    "name": "mlstar",
                    "interfaces": ["ILiquidHandler", "IProtocolRunner"],
                }),
            ],
        )

        assert registry.peek_interfaces("mlstar") == frozenset(
            {"ILiquidHandler", "IProtocolRunner"}
        )
        # Unknown device -> None (cold start; factory falls back to default).
        assert registry.peek_interfaces("nonexistent") is None

    async def test_list_devices(self, registry):
        """Test listing devices with filters."""
        devices1 = [
            DeviceConnectInfo.model_validate({"type": "shaker", "name": "shaker_1", "interfaces": ["IShaker"]}),
            DeviceConnectInfo.model_validate({"type": "centrifuge", "name": "centrifuge_1", "interfaces": ["ICentrifuge"]}),
        ]
        devices2 = [
            DeviceConnectInfo.model_validate({"type": "shaker", "name": "shaker_2", "interfaces": ["IShaker"]}),
        ]

        await registry.register_client(
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
            devices=devices1,
        )

        await registry.register_client(
            client_id="lab2-client",
            site="cambridge",
            lab="cellculture",
            workcell=None,
            devices=devices2,
        )

        # List all devices in the deployment
        all_devices = await registry.list_devices()
        assert len(all_devices) == 3

        # Filter by type
        shakers = await registry.list_devices(device_type="shaker")
        assert len(shakers) == 2

        # Filter by site
        boston_devices = await registry.list_devices(site="boston")
        assert len(boston_devices) == 2

        # Filter by lab
        molbio_devices = await registry.list_devices(lab="molbio")
        assert len(molbio_devices) == 2

        # Filter by status
        ready_devices = await registry.list_devices(status="ready")
        assert len(ready_devices) == 3

    async def test_the_agents_report_lands_on_the_device_snapshot(self, registry):
        """Status, link, and bring-up all come from the device bridge that
        holds the driver."""
        devices = [
            DeviceConnectInfo.model_validate({"type": "shaker", "name": "shaker_1", "interfaces": ["IShaker"]}),
        ]

        await registry.register_client(
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
            devices=devices,
        )

        fresh = await registry.get_device("shaker_1")
        assert fresh is not None
        assert fresh.is_connected is False, "nothing reported yet"
        assert fresh.link_mode is None

        await registry.apply_agent_report(
            "shaker_1",
            DeviceStatusInfo(
                status="busy",
                links={"LIVE": DeviceLinkInfo(is_connected=True, is_initialized=True)},
            ),
        )

        device = await registry.get_device("shaker_1")
        assert device is not None
        assert device.status == "busy"
        assert device.link_mode == "LIVE"
        assert device.is_connected is True
        assert device.is_initialized is True

    async def test_an_open_simulator_link_is_recorded_as_the_simulators(self, registry):
        """The roster answers no run mode, so it must say which driver answered.

        A DEVICE_SIM bench answers every command, and a reader shown only
        "connected" would report a simulator as the instrument.
        """
        await registry.register_client(
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
            devices=[
                DeviceConnectInfo.model_validate(
                    {"type": "shaker", "name": "shaker_1", "interfaces": ["IShaker"]},
                ),
            ],
        )

        await registry.apply_agent_report(
            "shaker_1",
            DeviceStatusInfo(
                status="ready",
                links={
                    "LIVE": DeviceLinkInfo(is_connected=False, is_initialized=False),
                    "DEVICE_SIM": DeviceLinkInfo(
                        is_connected=True, is_initialized=True,
                    ),
                },
            ),
        )

        device = await registry.get_device("shaker_1")
        assert device is not None
        assert device.is_connected is True
        assert device.link_mode == "DEVICE_SIM"

    async def test_a_report_for_an_unknown_device_is_dropped(self, registry):
        """A device nobody advertised has no row to write into."""
        await registry.apply_agent_report(
            "ghost_1",
            DeviceStatusInfo(
                status="ready",
                links={"LIVE": DeviceLinkInfo(is_connected=True, is_initialized=True)},
            ),
        )
        assert await registry.get_device("ghost_1") is None

    async def test_a_wire_shape_change_cannot_write_the_wrong_type_into_a_field(
        self, registry,
    ):
        """The roster is mutated field by field, so it validates on assignment.

        Without that, a protocol whose `status` stops being a plain string
        writes the whole record into the field and every reader downstream --
        the `status=` filter, the REST body -- is quietly wrong, with no test
        on either side of the wire able to see it.
        """
        await registry.register_client(
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
            devices=[
                DeviceConnectInfo.model_validate(
                    {"type": "shaker", "name": "shaker_1", "interfaces": ["IShaker"]},
                ),
            ],
        )
        snapshot = await registry.get_device("shaker_1")
        assert snapshot is not None

        with pytest.raises(pydantic.ValidationError):
            snapshot.status = DeviceStatusInfo(
                status="busy",
                links={"LIVE": DeviceLinkInfo(is_connected=True, is_initialized=True)},
            )

    async def test_update_heartbeat(self, registry):
        """Test updating client heartbeat."""
        devices = [
            DeviceConnectInfo.model_validate({"type": "shaker", "name": "shaker_1", "interfaces": ["IShaker"]}),
        ]

        await registry.register_client(
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
            devices=devices,
        )

        # Update heartbeat (should not raise error)
        await registry.update_heartbeat("lab1-client")

    async def test_is_device_online(self, registry):
        """Test checking if device is online."""
        devices = [
            DeviceConnectInfo.model_validate({"type": "shaker", "name": "shaker_1", "interfaces": ["IShaker"]}),
        ]

        await registry.register_client(
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
            devices=devices,
        )

        # Device online
        assert await registry.is_device_online("shaker_1") is True

        # Device offline (nonexistent)
        assert await registry.is_device_online("nonexistent") is False

        # Unregister and check again
        await registry.unregister_client("lab1-client")
        assert await registry.is_device_online("shaker_1") is False

    async def test_multiple_clients(self, registry):
        """Test multiple clients with overlapping device types."""
        devices1 = [
            DeviceConnectInfo.model_validate({"type": "shaker", "name": "lab1-shaker-01", "interfaces": ["IShaker"]}),
        ]
        devices2 = [
            DeviceConnectInfo.model_validate({"type": "shaker", "name": "lab2-shaker-01", "interfaces": ["IShaker"]}),
        ]

        await registry.register_client(
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
            devices=devices1,
        )

        await registry.register_client(
            client_id="lab2-client",
            site="cambridge",
            lab="cellculture",
            workcell=None,
            devices=devices2,
        )

        # Both clients active
        assert registry.get_active_client_count() == 2
        assert registry.get_active_device_count() == 2

        # Devices from different clients have different IDs
        client1 = await registry.get_client_for_device("lab1-shaker-01")
        client2 = await registry.get_client_for_device("lab2-shaker-01")
        assert client1 == "lab1-client"
        assert client2 == "lab2-client"
