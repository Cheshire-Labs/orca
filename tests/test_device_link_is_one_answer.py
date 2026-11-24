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

from orca.config import OrcaConfig
from typing import ClassVar

from cheshire_drivers.gateway_protocol import (
    DeviceConnectInfo,
    DeviceLinkInfo,
    DeviceStatusInfo,
)


from orca.gateway.connection_source import DeviceConnectionSource
from orca.gateway.registry.connection_tracker import DeviceConnectionTracker
from orca.resource_models.resource_pool import ResourcePool
from orca.modes.run_modes import OUT_OF_RUN_BASE, mode_scope
from orca.modes.device_modes import DeviceMode
from orca.runtime.registries.topology_registry import SystemTopologyRegistry
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from tests.device_modes import set_to
from tests.mock import UniversalMockDevice, UniversalSimDriver
from tests.test_helpers import wire_system_map
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.memory_activity import memory_unsaved_positions
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service





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


class _OneDriverMockDevice(UniversalMockDevice):
    """The mock, with its driver built by `driver_factory`."""

    driver_factory: ClassVar[type[UniversalSimDriver]] = UniversalSimDriver

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._driver = type(self).driver_factory(name)


class _AgentHeldMockDevice(_OneDriverMockDevice):
    """A device whose driver is a stand-in, the way a gateway build wires it."""

    driver_factory: ClassVar[type[UniversalSimDriver]] = _StandInDriver


async def _system_with_one_shaker(
    device_cls: type[_OneDriverMockDevice] = _OneDriverMockDevice,
    setting: DeviceMode = DeviceMode.LIVE,
) -> ISystem:
    device = device_cls("shaker_1")
    set_to(device, "shaker_1", setting)
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
        event_bus=EventBus(), config=OrcaConfig(), ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(),
        unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    return builder.get_system()


async def _bring_up(system: ISystem, *verbs: str) -> None:
    """What an operator's verbs do to the in-process driver, under the operator base."""
    device = system.get_device("shaker_1")
    with mode_scope(OUT_OF_RUN_BASE):
        for verb in verbs:
            await getattr(device, verb)()


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
        "lab1-client",
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
    return await memory_runtime(
        system, gateway_registry=source, connection_source=source,
    )


async def test_both_device_routes_report_the_same_bring_up_state() -> None:
    """The device bridge brought the shaker up; nothing touched orca's own
    driver."""
    runtime = await _runtime_with_agent_reporting(is_initialized=True)

    entry = await runtime.device_registry.get("shaker_1")
    assert entry is not None
    from_registry = await entry.is_initialized()
    from_snapshot = (await runtime.devices.get_device_status("shaker_1")).is_initialized
    from_inventory = {
        e.name: await e.is_initialized() for e in await runtime.device_registry.list_all()
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
    from_snapshot = (await runtime.devices.get_device_status("shaker_1")).is_initialized

    assert from_registry is False
    assert from_snapshot == from_registry


async def test_with_no_agent_both_routes_read_the_same_local_driver() -> None:
    """No device bridge means orca holds the driver, and both routes read that
    one."""
    system = await _system_with_one_shaker()
    runtime = await memory_runtime(system)
    await _bring_up(system, "initialize")

    entry = await runtime.device_registry.get("shaker_1")
    assert entry is not None
    from_registry = await entry.is_initialized()
    from_snapshot = (await runtime.devices.get_device_status("shaker_1")).is_initialized

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
    runtime = await memory_runtime(system)

    entry = await runtime.device_registry.get("shaker_1")
    assert entry is not None
    assert await entry.is_device_connected() is False, "nothing has run yet"

    await _bring_up(system, "initialize", "connect")

    entry = await runtime.device_registry.get("shaker_1")
    assert entry is not None
    assert await entry.is_device_connected() is True
    assert await entry.is_initialized() is True


async def test_the_answer_names_the_world_the_verbs_landed_in() -> None:
    """A simulator's open link must never be shown as the instrument's.

    Operator verbs mean the real device by default (D4), so on a bench with
    no device bridge the flags of a device set to live describe the live
    in-process driver and the mode says LIVE.
    """
    system = await _system_with_one_shaker()
    runtime = await memory_runtime(system)
    await _bring_up(system, "initialize")

    entry = await runtime.device_registry.get("shaker_1")
    assert entry is not None
    assert await entry.is_initialized() is True
    assert entry.device_link_mode() == "LIVE", (
        "the flags describe the driver the operator's verbs drove"
    )


async def test_a_device_set_to_sim_names_the_sim_world() -> None:
    """A device set to sim takes its verbs as SIM, its flags describe the slot
    those verbs drove, and only the mode says the open link is not the
    instrument's."""
    system = await _system_with_one_shaker(setting=DeviceMode.SIM)
    runtime = await memory_runtime(system)
    await _bring_up(system, "initialize")

    entry = await runtime.device_registry.get("shaker_1")
    assert entry is not None
    assert await entry.is_initialized() is True
    assert entry.device_link_mode() == "SIM", (
        "the flags describe a device set to sim, and only the mode says so"
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
    runtime = await memory_runtime(
        system, gateway_registry=source, connection_source=source,
    )
    # Bring the in-process stand-in up so it claims a live link; it is no
    # answer about the instrument, which is the point of the test.
    await _bring_up(system, "initialize")
    device = system.get_device("shaker_1")
    assert device.driver.is_connected, (
        "the local driver must claim a link, or dropping the device bridge proves nothing"
    )

    await tracker.apply_agent_report(
        "lab1-client",
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
    assert entry.device_link_mode() is None, "with no orca-client there is no world the flags describe"

