"""A bridge connected to the daemon's own gateway shows up on the daemon's reads.

The daemon serves `/ws/devices`, so a device bridge registers its devices in
`device_connection_tracker` inside the daemon process. `DeviceRegistryImpl`
reads connections through the `IDeviceConnectionSource` its `SystemRuntime` was
given, and with nothing given that is `NullDeviceConnectionSource`, which
answers "nothing connected" forever. The two halves then disagree inside one
process: `orca device capabilities` lists what the bridge advertised (it reads
the tracker directly) while `orca device registry show` calls the same device
not connected and eligible for no wire mode.

The test drives the whole seam -- mount a topology over REST, connect a bridge
over the websocket, read the registry row back over REST -- because the seam is
the defect: both halves pass their own suites.
"""

import time
from collections.abc import AsyncGenerator

import pytest
from cheshire_drivers import AccessConfig, CartesianCoordinates, Teachpoint
from fastapi.testclient import TestClient

from cheshire_drivers.gateway_protocol import (
    ConnectMessage,
    DeviceConnectInfo,
    DeviceLinkInfo,
    DeviceStatusInfo,
    MessageEnvelope,
    StatusMessage,
)
from orca.daemon.app import create_app
from orca.devices.shaker import Shaker
from orca.gateway.controller import device_controller
from orca.gateway.gateway_backed_driver import GatewayBackedDriver
from orca.gateway.registry import device_connection_tracker
from orca.gateway.registry.snapshot import DeviceSnapshot
from orca.gateway.websocket.connection_events import connection_events
from orca.gateway.websocket.manager import connection_manager
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from orca.sdk.workflow import MethodTemplate
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate
import orca.orca as orca


_TOPOLOGY_SPEC = f"{__name__}:build_topology"
_WORKFLOW_SPEC = f"{__name__}:build_workflow"

# Declared by `build_topology` below. A bridge advertising a name the topology
# does not declare is the other case, covered by the second test.
_DECLARED_DEVICE = "shaker_1"

_POSITIONS = ("shaker_1", "pad_1")


def build_topology(stores: IRuntimeStoreFactory) -> Topology:
    """One shaker, one pad, one arm. Mounted through `POST /mount-topology`.

    A real `Shaker` rather than a test mock: the connect handshake checks the
    bridge's advertised interfaces against the topology's declared set, and a
    mock declaring every interface would be refused before it registers.
    """
    access = AccessConfig(name="default_vertical", access_type="vertical")
    stores.access_configs(seed=[access])
    teachpoints = [
        Teachpoint(
            position_id=name,
            coordinates=CartesianCoordinates(
                x=float(index * 50 + 100), y=0.0, z=50.0,
                yaw=180.0, pitch=90.0, roll=0.0,
            ),
            orientation="right",
            access=access,
        )
        for index, name in enumerate(_POSITIONS)
    ]
    return Topology(
        locations={"shaker_1": Shaker("shaker_1"), "pad_1": PlatePad("pad_1")},
        transporters=[
            Transporter(
                "robot_1",
                teachpoint_store=stores.teachpoints("robot_1", seed=teachpoints),
            ),
        ],
    )


def build_workflow(topology: Topology) -> WorkflowTemplate:
    """Shake a plate from the pad once."""
    plate = PlateTemplate("plate_96", labware_type="Cor_96_wellplate_360ul_Fb")
    shaker = topology.device("shaker_1", Shaker)

    @orca.action(device=shaker, inputs=[plate])
    async def shake(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake

    @orca.thread(labware=plate, start="pad_1", end="pad_1")
    async def plate_journey(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield shake_method

    @orca.workflow(name="shake_once")
    def shake_once(wf: WorkflowContext) -> None:
        wf.start(plate_journey)

    return shake_once


@pytest.fixture(autouse=True)
def _clean_singletons():
    """The connection manager and the tracker are process singletons."""
    yield
    connection_manager._connections.clear()
    device_connection_tracker._clients.clear()
    device_connection_tracker._devices.clear()


def _connect_envelope(name: str) -> str:
    msg = ConnectMessage(
        site="test",
        lab="simulation",
        devices=[
            DeviceConnectInfo(
                name=name, type="shaker", interfaces=frozenset({"IShaker"}),
            ),
        ],
    )
    return MessageEnvelope.wrap_connect(msg).model_dump_json()


def _status_envelope(name: str) -> str:
    msg = StatusMessage(
        devices={
            name: DeviceStatusInfo(
                status="ready",
                links={
                    "LIVE": DeviceLinkInfo(is_connected=True, is_initialized=True),
                },
            ),
        },
        timestamp=1.0,
    )
    return MessageEnvelope.wrap_status(msg).model_dump_json()


def _await_reported(name: str, timeout: float = 10.0) -> DeviceSnapshot:
    """Wait for the daemon's loop to apply the bridge's status report.

    The app runs on TestClient's own event loop, so the tracker's lock belongs
    to a different loop than the test's. `peek_snapshot` is the sync accessor
    for exactly that.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = device_connection_tracker.peek_snapshot(name)
        if snapshot is not None and snapshot.link_mode is not None:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"no status applied for {name!r} within {timeout}s")


def test_a_bridge_on_the_daemons_gateway_reads_as_connected() -> None:
    with TestClient(create_app()) as client:
        mount = client.post(
            "/mount-topology", json={"spec": _TOPOLOGY_SPEC, "sim": False},
        )
        assert mount.status_code == 200, mount.text
        try:
            with client.websocket_connect("/ws/devices") as ws:
                ws.send_text(_connect_envelope(_DECLARED_DEVICE))
                ws.send_text(_status_envelope(_DECLARED_DEVICE))
                _await_reported(_DECLARED_DEVICE)

                row = client.get(f"/devices/registry/{_DECLARED_DEVICE}")
                assert row.status_code == 200, row.text
                body = row.json()
                ws.close()
        finally:
            client.post("/unload", json={})

    card = body["connection_card"]
    assert card is not None, "a connected bridge must produce a connection card"
    assert card["advertised_kind"] == "shaker"
    assert body["is_client_connected"] is True
    assert body["is_device_connected"] is True
    assert body["is_initialized"] is True
    assert body["device_link_mode"] == "LIVE"
    eligibility = body["mode_eligibility"]
    assert eligibility["device_sim"] is True
    assert eligibility["live"] is True


def test_a_bridge_only_device_is_listed_beside_the_declared_ones() -> None:
    """`list_all` composes both sources, so a name the topology never declared
    still gets a row. Reading topology alone hides the misnamed device the
    operator actually connected, which is the usual reason a declared one
    reads as absent."""
    with TestClient(create_app()) as client:
        mount = client.post(
            "/mount-topology", json={"spec": _TOPOLOGY_SPEC, "sim": False},
        )
        assert mount.status_code == 200, mount.text
        try:
            with client.websocket_connect("/ws/devices") as ws:
                ws.send_text(_connect_envelope("shaker_typo"))
                ws.send_text(_status_envelope("shaker_typo"))
                _await_reported("shaker_typo")

                listing = client.get("/devices/registry")
                assert listing.status_code == 200, listing.text
                names = [row["name"] for row in listing.json()["devices"]]
                ws.close()
        finally:
            client.post("/unload", json={})

    assert "shaker_typo" in names
    assert _DECLARED_DEVICE in names


def test_a_mounted_device_is_driven_through_the_bridge() -> None:
    """The daemon builds devices through orca-client: the live driver of every
    mounted device is reached over `/ws/devices`, never run inside the daemon."""
    app = create_app()
    with TestClient(app) as client:
        mount = client.post(
            "/mount-topology", json={"spec": _TOPOLOGY_SPEC, "sim": False},
        )
        assert mount.status_code == 200, mount.text
        try:
            system = app.state.system_runtime.system
            shaker = system.get_device(_DECLARED_DEVICE)
            arm = next(t for t in system.transporters if t.name == "robot_1")
            live_drivers = {
                "shaker_1": type(shaker.live_driver),
                "robot_1": type(arm.live_driver),
            }
        finally:
            client.post("/unload", json={})

    for name, driver_class in live_drivers.items():
        assert issubclass(driver_class, GatewayBackedDriver), (name, driver_class)


def test_the_daemon_hands_bridge_connects_and_drops_to_the_controller() -> None:
    """A dropped bridge starts the controller's disconnect grace, and a returning
    one resumes its commands. The hooks leave with the app."""
    before_connected = list(connection_events._connected)
    before_disconnected = list(connection_events._disconnected)
    before_grace = device_controller._disconnect_grace
    with TestClient(create_app()):
        assert device_controller.on_device_disconnected in connection_events._disconnected
        assert len(connection_events._connected) == len(before_connected) + 1
        assert device_controller._disconnect_grace is not before_grace
        during_grace = device_controller._disconnect_grace
    assert connection_events._connected == before_connected
    assert connection_events._disconnected == before_disconnected
    assert device_controller._disconnect_grace is not during_grace


def test_with_no_bridge_the_daemon_refuses_device_sim_and_live_naming_the_devices() -> None:
    """PURE_SIM needs no orca-client; DEVICE_SIM and LIVE do."""
    with TestClient(create_app()) as client:
        mount = client.post("/mount-topology", json={"spec": _TOPOLOGY_SPEC, "sim": False})
        assert mount.status_code == 200, mount.text
        load = client.post("/workflows", json={"spec": _WORKFLOW_SPEC})
        assert load.status_code == 200, load.text
        try:
            runs = {
                mode: client.post(
                    "/executions", json={"workflow_name": "shake_once", "run_mode": mode},
                )
                for mode in ("PURE_SIM", "DEVICE_SIM", "LIVE")
            }
        finally:
            client.post("/unload", json={})

    assert runs["PURE_SIM"].status_code == 200, runs["PURE_SIM"].text
    for mode in ("DEVICE_SIM", "LIVE"):
        assert runs[mode].status_code == 409, runs[mode].text
        assert "shaker_1" in runs[mode].text and "robot_1" in runs[mode].text
