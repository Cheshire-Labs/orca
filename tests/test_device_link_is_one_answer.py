"""One device, one answer about its link, whoever is asking and whoever answers.

Two halves. First, the routes must agree: `GET /devices/registry/{name}` (via
`runtime.device_registry`) and `GET /devices/{name}` (via `runtime.devices`)
both report whether a device has been brought up, and `orca device registry
show` / `orca device show` print them side by side. They used to source that
from different places, so they could contradict each other for the same device
at the same instant.

Second, the answer must come from whoever is actually driving the device, and
say which one that was. With a device bridge, that is the device bridge. With
none, it is the driver `connect` / `initialize` / `disconnect` dispatch through,
so an operator sees what their own verbs did, with the mode saying which world
they landed in.
When a device bridge holds the device but has gone quiet, nobody can answer,
and the stand-in it left behind holds a cache that must not stand in for one.
"""

from typing import ClassVar, Optional, cast

from cheshire_drivers.gateway_protocol import (
    DeviceConnectInfo,
    DeviceLinkInfo,
    DeviceStatusInfo,
)

from pydantic import JsonValue

from orca.devices.devices import Storage
from orca.gateway.connection_source import DeviceConnectionSource
from orca.gateway.controller.controller import DeviceController
from orca.gateway.remote_device_factory import RemoteDeviceFactory
from orca.gateway.registry.connection_tracker import DeviceConnectionTracker
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.simulation_manager import SimulationManager
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.registries.device_registry import DeviceRegistryImpl
from orca.runtime.registries.topology_registry import SystemTopologyRegistry
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.status_models import TopologyDeviceEntry
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from tests.mock import UniversalMockDevice, UniversalSimDriver
from tests.test_helpers import wire_system_map


class _UncalledController:
    """Stands in for the controller. Building drivers must not dispatch."""

    async def execute_command(self, **kwargs: object) -> Optional[JsonValue]:
        raise AssertionError("building a driver pair must not send a command")


class _OneDeviceTopology:
    """Topology source declaring exactly one device."""

    def __init__(self, name: str, kind: str, interfaces: tuple[str, ...]) -> None:
        self._entry = TopologyDeviceEntry(
            name=name, kind=kind, interfaces=interfaces,
            position_ids=(name,), sim_override=None,
            interfaces_are_class_defaults=False,
        )

    def list_devices(self) -> list[TopologyDeviceEntry]:
        return [self._entry]

    def get_device(self, name: str) -> TopologyDeviceEntry | None:
        return self._entry if name == self._entry.name else None


class _OneResourceSystem:
    """Resource lookup holding exactly one device."""

    def __init__(self, name: str, resource: Storage) -> None:
        self._name = name
        self._resource = resource

    def has_resource(self, name: str) -> bool:
        return name == self._name

    def get_resource(self, name: str) -> Storage:
        return self._resource


class _StandInDriver(UniversalSimDriver):
    """A driver already carrying the factory's agent-held stamp.

    Declares the attribute rather than being stamped, so this file can build
    one without a factory. It starts brought up, which is the cache a device
    leaves behind after a workflow has run through it.
    """

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._is_initialized = True

    @property
    def instrument_is_held_remotely(self) -> bool:
        return True


class _TwoDriverMockDevice(UniversalMockDevice):
    """The mock, but with the live and sim slots held by different objects.

    `UniversalMockDevice` passes one driver as both, so `resource.driver` and
    `resource.live_driver` are the same object and any test asking which one a
    read consults cannot fail.
    """

    live_driver_factory: ClassVar[type[UniversalSimDriver]] = UniversalSimDriver

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._sim_manager = SimulationManager(
            type(self).live_driver_factory(name),
            UniversalSimDriver(f"{name}_sim"),
        )


class _AgentHeldMockDevice(_TwoDriverMockDevice):
    """A device whose live slot is a stand-in, the way a gateway build wires it."""

    live_driver_factory: ClassVar[type[UniversalSimDriver]] = _StandInDriver


class _DeclaredSimMockDevice(_TwoDriverMockDevice):
    """Two slots AND a topology sim_override pinning the device to its sim."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._sim_manager = SimulationManager(
            UniversalSimDriver(name),
            UniversalSimDriver(f"{name}_sim"),
            sim_override=WorkflowRunMode.PURE_SIM,
        )


async def _system_with_one_shaker(
    device_cls: type[_TwoDriverMockDevice] = _TwoDriverMockDevice,
) -> ISystem:
    device = device_cls("shaker_1")
    assert device.driver is not device.live_driver, (
        "the two slots must hold different objects, or nothing below can fail"
    )
    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource_pool(ResourcePool("shaker_1", [device]))

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker_1": device}, pads=["pad1"])

    builder = SdkToSystemBuilder(
        name="device_link_one_answer",
        description="",
        labwares=[],
        resources_registry=registry,
        system_map=system_map,
        workflows=[],
        event_bus=EventBus(),
    )
    await builder.bind_labwares()
    return builder.get_system()


async def _runtime_with_agent_reporting(
    *, is_initialized: bool,
) -> SystemRuntime:
    system = await _system_with_one_shaker()
    declared = SystemTopologyRegistry(system).get_device("shaker_1")
    assert declared is not None

    tracker = DeviceConnectionTracker()
    await tracker.register_client(
        client_id="lab1-client",
        site="boston",
        lab="molbio",
        workcell=None,
        devices=[
            DeviceConnectInfo(
                name="shaker_1",
                type=declared.kind,
                interfaces=frozenset(declared.interfaces),
            ),
        ],
    )
    await tracker.apply_agent_report(
        "shaker_1",
        DeviceStatusInfo(
            status="ready",
            links={
                "LIVE": DeviceLinkInfo(
                    is_connected=True, is_initialized=is_initialized,
                ),
            },
        ),
    )
    source = DeviceConnectionSource(tracker)
    return SystemRuntime(
        system, gateway_registry=source, connection_source=source,
    )


async def test_both_device_routes_report_the_same_bring_up_state() -> None:
    """The device bridge brought the shaker up; nothing touched orca's own
    driver."""
    runtime = await _runtime_with_agent_reporting(is_initialized=True)

    entry = await runtime.device_registry.get("shaker_1")
    assert entry is not None
    from_registry = await entry.is_initialized()
    from_snapshot = runtime.devices.get_device_status("shaker_1").is_initialized
    from_inventory = {
        d.name: d.is_initialized for d in runtime.registry.list_devices()
    }["shaker_1"]

    assert from_registry is True, "the device bridge reported the device brought up"
    assert from_snapshot == from_registry
    assert from_inventory == from_registry


async def test_the_routes_still_agree_when_the_agent_reports_not_brought_up() -> None:
    """The agreement has to hold in both directions, not just where they'd match."""
    runtime = await _runtime_with_agent_reporting(is_initialized=False)

    entry = await runtime.device_registry.get("shaker_1")
    assert entry is not None
    from_registry = await entry.is_initialized()
    from_snapshot = runtime.devices.get_device_status("shaker_1").is_initialized

    assert from_registry is False
    assert from_snapshot == from_registry


async def test_with_no_agent_both_routes_read_the_same_local_driver() -> None:
    """No device bridge means orca holds the driver, and both routes read that
    one."""
    system = await _system_with_one_shaker()
    runtime = SystemRuntime(system)
    await runtime.devices.initialize("shaker_1", confirm=True)

    entry = await runtime.device_registry.get("shaker_1")
    assert entry is not None
    from_registry = await entry.is_initialized()
    from_snapshot = runtime.devices.get_device_status("shaker_1").is_initialized

    assert from_registry is True, "the in-process driver was brought up"
    assert from_snapshot == from_registry


async def test_the_operators_own_verbs_move_the_flags_they_are_shown() -> None:
    """`connect` and `initialize` must move what `orca device registry` prints.

    Both verbs dispatch through the device's run-mode-picked driver against
    the operator write base (LIVE by default, D4). A read that named the
    other slot would leave the operator running verbs that return 200 and
    change nothing they can see.
    """
    system = await _system_with_one_shaker()
    runtime = SystemRuntime(system)

    entry = await runtime.device_registry.get("shaker_1")
    assert entry is not None
    assert await entry.is_device_connected() is False, "nothing has run yet"

    await runtime.devices.initialize("shaker_1", confirm=True)
    await runtime.devices.connect("shaker_1")

    entry = await runtime.device_registry.get("shaker_1")
    assert entry is not None
    assert await entry.is_device_connected() is True
    assert await entry.is_initialized() is True


async def test_the_answer_names_the_world_the_verbs_landed_in() -> None:
    """A simulator's open link must never be shown as the instrument's.

    Operator verbs mean the real device by default (D4), so on a bench with
    no device bridge the flags describe the live in-process driver and the mode
    says LIVE. A device the topology declares sim keeps its verbs on the
    simulator, and only the mode says so.
    """
    system = await _system_with_one_shaker()
    runtime = SystemRuntime(system)
    await runtime.devices.initialize("shaker_1", confirm=True)

    entry = await runtime.device_registry.get("shaker_1")
    assert entry is not None
    assert await entry.is_initialized() is True
    assert entry.device_link_mode() == "LIVE", (
        "the flags describe the driver the operator's verbs drove"
    )


async def test_a_declared_sim_device_names_the_sim_world() -> None:
    """The ratchet's read side: a topology-declared sim device keeps its verbs
    on the simulator, its flags describe that slot, and only the mode says the
    open link is not the instrument's."""
    system = await _system_with_one_shaker(_DeclaredSimMockDevice)
    runtime = SystemRuntime(system)
    await runtime.devices.initialize("shaker_1", confirm=True)

    entry = await runtime.device_registry.get("shaker_1")
    assert entry is not None
    assert await entry.is_initialized() is True
    assert entry.device_link_mode() == "PURE_SIM", (
        "the flags describe orca's simulator, and only the mode says so"
    )


async def test_an_agent_going_quiet_does_not_promote_its_stand_ins_cache() -> None:
    """The dangerous direction: a dropped device bridge must not read as
    connected.

    `unregister_client` deletes the device row, so "the device bridge went away"
    and "there was never a device bridge" arrive looking the same. What
    separates them is the driver left in process: on a gateway deployment it is
    a stand-in whose link flags are whatever last passed through it on the way
    to the wire.
    """
    tracker = DeviceConnectionTracker()
    system = await _system_with_one_shaker(_AgentHeldMockDevice)
    declared = SystemTopologyRegistry(system).get_device("shaker_1")
    assert declared is not None
    await tracker.register_client(
        client_id="lab1-client", site="boston", lab="molbio", workcell=None,
        devices=[DeviceConnectInfo(
            name="shaker_1", type=declared.kind,
            interfaces=frozenset(declared.interfaces),
        )],
    )
    source = DeviceConnectionSource(tracker)
    runtime = SystemRuntime(
        system, gateway_registry=source, connection_source=source,
    )
    # Bring both in-process slots up so each claims a live link; neither is
    # an answer about the instrument, which is the point of the test.
    await runtime.devices.initialize("shaker_1", confirm=True)
    await runtime.devices.initialize(
        "shaker_1", mode=WorkflowRunMode.PURE_SIM, confirm=True,
    )
    device = system.get_device("shaker_1")
    assert (
        device.driver_under(WorkflowRunMode.PURE_SIM).is_connected
        and device.live_driver.is_connected
    ), (
        "both local drivers must claim a link, or dropping the device bridge "
        "proves nothing"
    )

    await tracker.apply_agent_report(
        "shaker_1",
        DeviceStatusInfo(
            status="ready",
            links={"LIVE": DeviceLinkInfo(
                is_connected=False, is_initialized=False,
            )},
        ),
    )
    entry = await runtime.device_registry.get("shaker_1")
    assert entry is not None
    assert await entry.is_device_connected() is False, "the device bridge said the link is closed"

    await tracker.unregister_client("lab1-client")

    entry = await runtime.device_registry.get("shaker_1")
    assert entry is not None
    assert await entry.is_device_connected() is False, (
        "with the device bridge gone nothing in process can answer, and a "
        "stale cache reporting the arm connected is worse than reporting it "
        "closed"
    )
    assert await entry.is_initialized() is False


async def test_a_passive_agent_held_device_reads_unknown_when_its_agent_drops() -> None:
    """Storage and waste get no proxy, and must still not answer for themselves.

    Their interfaces declare no methods, so `RemoteDeviceFactory` puts a local
    simulator in the live slot rather than a wire proxy. They are still
    agent-held: an agent advertises them, and orca's own bring-up walk is what
    flips that simulator's flags. Deciding "is an agent behind this" by looking
    for a proxy therefore missed them, and a dropped agent handed the operator
    a connected, brought-up device with nothing behind it.
    """
    tracker = DeviceConnectionTracker()
    factory = RemoteDeviceFactory(
        controller=cast(DeviceController, _UncalledController()),
        default_timeout=30.0,
        mode_resolver=lambda _name: WorkflowRunMode.LIVE,
    )
    with use_device_factory(factory):
        storage = Storage("storage_1")

    await tracker.register_client(
        client_id="lab1-client", site="boston", lab="molbio", workcell=None,
        devices=[DeviceConnectInfo(
            name="storage_1", type="storage", interfaces=frozenset({"IStorage"}),
        )],
    )
    source = DeviceConnectionSource(tracker)
    registry = DeviceRegistryImpl(
        topology_source=_OneDeviceTopology("storage_1", "storage", ("IStorage",)),
        connection_source=source,
        system=_OneResourceSystem("storage_1", storage),
    )

    # The bring-up walk initializes whatever dispatch resolves to, which for a
    # passive device is that same local simulator.
    await storage.initialize()
    assert storage.driver.is_initialized, (
        "the local driver must claim bring-up, or dropping the device bridge "
        "proves nothing"
    )

    await tracker.apply_agent_report(
        "storage_1",
        DeviceStatusInfo(
            status="ready",
            links={"LIVE": DeviceLinkInfo(is_connected=False, is_initialized=False)},
        ),
    )
    entry = await registry.get("storage_1")
    assert entry is not None
    assert await entry.is_device_connected() is False, "the device bridge said the link is closed"

    await tracker.unregister_client("lab1-client")

    entry = await registry.get("storage_1")
    assert entry is not None
    assert await entry.is_device_connected() is False
    assert await entry.is_initialized() is False
    assert entry.device_link_mode() is None, (
        "nobody can answer for an agent-held device whose agent is gone"
    )
