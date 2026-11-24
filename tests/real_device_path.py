"""The one way tests build devices: the check run's device stack, with every run mode on the simulator.

A command goes forwarding driver, device controller, in-process orca-client, cheshire-drivers executor, simulator.
It is the lab path without the websocket hop to a real orca-client. The autouse fixture in `conftest.py` binds one
`RealDevicePath` for every test.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import TypeVar

from cheshire_drivers.driver_lookup import Driver, DriverRequest, build_sim_driver
from cheshire_drivers.gateway_protocol import AssignedDevice, DriverMode, EffectiveMode
from cheshire_drivers.device_setup import BUILTIN_CLIENT, DeviceSetup

from orca.driver_management.device_factory_context import use_device_factory
from orca.driver_management.device_factory_protocol import DeviceDriver, IDeviceDriverProvider
from orca.gateway.mode_resolution import HasSystem, ResourceLookup
from orca.resource_models.resources import IResource
from orca.gateway.device_gateway import DeviceGateway
from orca.gateway.in_process_client import InProcessClient
from orca.resource_models.devices import Device
from orca.resource_models.transporter import Transporter
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.device_assignment import assign_devices, interfaces_from_setups, simulator_interfaces_from_setups
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.server_events import ServerEvents
from orca.system.system_interface import ISystem
from orca.runtime.startup import CheckRunDevices, DeviceConnections, PackageSetupStoreFactory, build_check_run_devices
from orca.runtime.store_factory import default_package_root


class _Built:
    def __init__(self, system: ResourceLookup) -> None:
        self.system = system


class _Unbuilt:
    """The devices a test connected with no system built: each answers its own mode."""

    def __init__(self) -> None:
        self.devices: dict[str, Device | Transporter] = {}

    @property
    def system(self) -> ResourceLookup:
        return self

    def has_resource(self, name: str) -> bool:
        return name in self.devices

    def get_resource(self, name: str) -> IResource:
        return self.devices[name]


class _RecordsWhatItBuilds:
    """The check run's device factory, noting each device it builds so a test can connect them all."""

    def __init__(self, factory: IDeviceDriverProvider) -> None:
        self._factory = factory
        self.built: dict[str, tuple[str, bool]] = {}

    def build_driver(self, device_type: str, name: str, *, deck_modeling: bool = False) -> DeviceDriver:
        self.built[name] = (device_type, deck_modeling)
        return self._factory.build_driver(device_type, name, deck_modeling=deck_modeling)


def _with_default_client(setup: DeviceSetup, default_client: str) -> DeviceSetup:
    return setup if setup.client is not None else setup.model_copy(update={"client": default_client})


# Tests send SIM and LIVE commands through the check run's client too, so every mode runs on the simulator.
_EVERY_MODE_ON_THE_SIMULATOR: Mapping[EffectiveMode, DriverMode] = MappingProxyType(
    {"CHECK_RUN": "SIM", "SIM": "SIM", "LIVE": "SIM"},
)


class RealDevicePath:
    """A check run's controller and in-process orca-client, and a factory whose devices expect what the test's run mode would."""

    def __init__(self, stores: PackageSetupStoreFactory, events: ServerEvents, run_mode: WorkflowRunMode | None = None) -> None:
        self.stores = stores
        self.events = events
        self._systems: list[HasSystem] = []
        self._unbuilt = _Unbuilt()
        self._simulators: dict[str, Driver] = {}
        self._connected: dict[str, AssignedDevice] = {}
        # A check run's devices expect their simulator's interfaces. Others stand in for sim and live ones, so they
        # expect what a lab PC's would.
        self._devices: CheckRunDevices = build_check_run_devices(
            stores, self._current_system, events,
            lambda tracker: InProcessClient(tracker, self._simulator, _EVERY_MODE_ON_THE_SIMULATOR),
            expected_interfaces=(
                simulator_interfaces_from_setups if run_mode is WorkflowRunMode.CHECK_RUN else interfaces_from_setups
            ),
        )
        self._factory = _RecordsWhatItBuilds(self._devices.device_factory)

    def _current_system(self) -> HasSystem:
        return self._systems[-1] if self._systems else self._unbuilt

    def _simulator(self, request: DriverRequest) -> Driver:
        return self._simulators.get(request.name) or build_sim_driver(request)

    @property
    def device_factory(self) -> IDeviceDriverProvider:
        return self._factory

    @contextmanager
    def building(self) -> Iterator[None]:
        """Devices built in the block get the check run's forwarding drivers."""
        with use_device_factory(self.device_factory):
            yield

    def simulate_with(self, name: str, driver: Driver) -> None:
        """The in-process orca-client runs `name`'s commands on `driver` instead of the simulator its setup names."""
        self._simulators[name] = driver

    def set_up(self, name: str, setup: DeviceSetup) -> None:
        """Write `name`'s setup file, which says what its driver declares and which orca-client runs it."""
        devices = self.stores.root / "devices"
        devices.mkdir(exist_ok=True)
        (devices / f"{name}.json").write_text(setup.model_dump_json(), encoding="utf-8")

    def simulator(self, name: str) -> Driver:
        """The driver the in-process orca-client runs `name`'s commands on."""
        driver = self._devices.client.simulator(name)
        if driver is None:
            raise KeyError(f"{name!r} is not connected to the in-process orca-client")
        return driver

    @property
    def connections(self) -> DeviceConnections:
        return self._devices.connections

    @property
    def gateway(self) -> DeviceGateway:
        return self._devices.connections.gateway

    async def drop(self, name: str) -> None:
        """`name`'s orca-client connection drops, as the websocket layer tells the controller."""
        await self.gateway.controller.on_device_disconnected(name)

    async def come_back(self, name: str) -> None:
        """`name`'s orca-client connection is back, as the websocket layer tells the controller."""
        await self.gateway.controller.on_device_reconnected(name)

    async def serve(self, runtime: ISystemRuntime) -> None:
        """`runtime` answers each device's mode, and the in-process orca-client runs every device of its system."""
        self._systems.append(runtime)
        await self._connect_all(runtime.system)

    async def connect_system(self, system: ISystem) -> None:
        """Run `system`'s devices on the in-process orca-client with no runtime built. `system` answers their modes."""
        self._systems.append(_Built(system))
        await self._connect_all(system)

    async def _connect_all(self, system: ISystem) -> None:
        await self.connect(assign_devices([*system.devices, *system.transporters], self.stores.device_setups()))

    async def connect_devices(self, *devices: Device | Transporter) -> None:
        """Run `devices` on the in-process orca-client with no system built. Each answers its own mode."""
        self._unbuilt.devices.update((device.name, device) for device in devices)
        await self.connect(assign_devices(devices, self.stores.device_setups()))

    async def connect_built(self) -> None:
        """Run every device built so far on the in-process orca-client. Each runs in the run's mode."""
        store = self.stores.device_setups()
        await self.connect([
            AssignedDevice(
                name=name, kind=kind, deck_modeling=deck_modeling,
                setup=_with_default_client(store.read(name), store.default_client),
            )
            for name, (kind, deck_modeling) in self._factory.built.items()
        ])

    async def connect(self, assignment: list[AssignedDevice]) -> None:
        """Assign the devices connected so far plus these, as a server sends one whole assignment."""
        new = {device.name: device for device in assignment if device.name not in self._connected}
        if new:
            self._connected.update(new)
            await self._devices.client.run(list(self._connected.values()))

    async def aclose(self) -> None:
        await self._devices.client.aclose()


_current: ContextVar[RealDevicePath | None] = ContextVar("_real_device_path", default=None)


def the_real_device_path() -> RealDevicePath:
    """The test's `RealDevicePath`, bound by the autouse fixture in `conftest.py`."""
    path = _current.get()
    if path is None:
        raise RuntimeError("no real device path is bound; the conftest autouse fixture binds one")
    return path


@asynccontextmanager
async def real_device_path(
    stores: PackageSetupStoreFactory | None = None, run_mode: WorkflowRunMode | None = None,
) -> AsyncIterator[RealDevicePath]:
    """Bind a `RealDevicePath` for the block. Every device built in it gets the check run's forwarding driver."""
    setup = stores or PackageSetupStoreFactory(default_package_root(), default_client=BUILTIN_CLIENT)
    path = RealDevicePath(setup, ServerEvents(), run_mode)
    token = _current.set(path)
    try:
        with path.building():
            yield path
    finally:
        _current.reset(token)
        await path.aclose()


_DeviceT = TypeVar("_DeviceT", bound=Device | Transporter)


async def connected(device: _DeviceT) -> _DeviceT:
    """`device`, run by the test's in-process orca-client with no system built."""
    await the_real_device_path().connect_devices(device)
    return device


_RunT = TypeVar("_RunT")


async def on_its_own_device_path(run: Callable[[], Awaitable[_RunT]]) -> _RunT:
    """`run()` with a `RealDevicePath` of its own, for a module fixture that runs in its own event loop."""
    async with real_device_path():
        return await run()
