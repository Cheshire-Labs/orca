"""Startup code both servers share: the gateway, the setup stores, the activity services, and the SystemRuntime.

Each server calls these once, at its own startup, and passes what they return
to everything that needs it. The servers differ only in the database under the
stores they pass in. Concrete classes are named here and in each
server's own startup.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypedDict

from orca.runtime.event_saves import EventSaves
from orca.config import SYSTEM_CONFIG_FILE, OrcaConfig
from orca.runtime.setup_files import read_system_config
from orca.variables.variable_definition import GLOBAL_VARIABLES_FILE
from orca.runtime.global_variable_service import GlobalVariableService, IGlobalVariableService
from orca.events.event_bus import EventBus
from orca.events.event_bus_interface import IEventBus
from orca.gateway.connection_source import DeviceConnectionSource
from orca.gateway.controller.command_clock import DeviceCommandClock
from orca.gateway.controller.controller import DeviceController
from orca.gateway.controller.disconnect_grace import DisconnectGrace
from orca.gateway.device_gateway import DeviceGateway, ServerDeviceGateway
from orca.gateway.in_process_client import InProcessClient
from orca.gateway.mode_resolution import HasSystem, system_mode_resolver
from orca.gateway.device_command_runner import IDeviceCommandRunner
from orca.gateway.operator_commands import OperatorDeviceCommands
from orca.gateway.registry.connection_tracker import DeviceConnectionTracker
from orca.gateway.remote_device_factory import RemoteDeviceFactory
from orca.gateway.websocket.assignments import DeviceAssignments
from orca.gateway.websocket.connection_events import ConnectionEventBus
from orca.gateway.websocket.manager import ConnectionManager, IClientChannel
from orca.plugins.async_offloading import AsyncOffloadingPlugin
from orca.plugins.base import OrcaPlugin
from orca.plugins.config import PluginConfig
from orca.plugins.webhook import WebhookPlugin
from orca.resource_models.resources import IModeAware
from orca.runtime.access_config_service import AccessConfigService, IAccessConfigService
from orca.runtime.deck_layout_service import DeckLayoutService, IDeckLayoutService
from orca.runtime.device_assignment import interfaces_from_setups, simulator_interfaces_from_setups
from orca.modes.device_modes import IDeviceModes
from orca.runtime.deployment_profile_service import (
    DeploymentProfileService,
    IDeploymentProfileService,
)
from orca.runtime.audit_store import IAuditStore
from orca.runtime.deployment_registries import DeploymentRegistries, IDeploymentRegistries
from orca.driver_management.device_factory_protocol import IDeviceDriverProvider
from orca.runtime.event_store import IEventStore
from orca.runtime.execution_record_service import ExecutionRecordService, IExecutionRecordService
from orca.runtime.execution_record_store import IExecutionRecordStore
from orca.runtime.facades.access_configs import AccessConfigFacade
from orca.runtime.facades.deck_layouts import DeckLayoutFacade
from orca.runtime.device_setup_service import DeviceSetupService, IDeviceSetupService
from orca.runtime.facades.device_modes import DeviceModeFacade
from orca.runtime.facades.device_setups import DeviceSetupFacade
from orca.runtime.facades.grip_profiles import GripProfileFacade
from orca.runtime.facades.move_defaults import MoveDefaultsFacade
from orca.runtime.facades.ops_history import OpsHistoryFacade
from orca.runtime.facades.profiles import DeploymentProfileFacade
from orca.runtime.facades.teachpoints import TeachpointFacade
from orca.runtime.facades.variables import VariableFacade
from orca.runtime.grip_profile_service import GripProfileService, IGripProfileService
from orca.runtime.incident_service import IIncidentService, IncidentService
from orca.runtime.incident_store import IIncidentStore
from orca.resource_models.store_interfaces import IDeviceSetupStore, ILabwareStore
from orca.events.emitter import IEventEmitter, IEventSink, IServerEvents
from orca.runtime.labware_catalog import StoreBackedLabwareCatalog
from orca.resource_models.labware_catalog_protocol import ILabwareCatalog
from orca.runtime.labware_catalog_service import ILabwareCatalogService, LabwareCatalogService
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placement import IUnsavedPositions, PositionNotSavedError
from orca.resource_models.labware_service_interface import ILabwareService
from orca.runtime.labware_service import AnnouncedLabwareService, LabwareService
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.log_store_writer import AuditWriter, EventWriter, LogStoreWriter
from orca.runtime.unsaved_record import IUnsavedRecordReporter, UnsavedPositions, UnsavedRecordReporter
from orca.runtime.move_defaults_service import IMoveDefaultsService, MoveDefaultsService
from orca.runtime.profile_store import FileDeploymentProfileStore
from orca.runtime.registries import DeviceRegistryImpl, SystemTopologyRegistry
from orca.modes.run_modes import OUT_OF_RUN_BASE, BaseMode, WorkflowRunMode
from orca.runtime.registry_protocols import (
    IDeviceConnectionSource,
    IGatewayRegistry,
)
from orca.runtime.runtime_interface import (
    ISystemRuntime,
)
from orca.runtime.setup_file_stores import (
    GlobalVariableFileStore,
    AccessConfigFileStore,
    DeckLayoutFileStore,
    DeviceSetupFileStore,
    GripProfileFileStore,
    LabwareCatalogFileStore,
    MoveDefaultsFileStore,
    TeachpointFileStore,
)
from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.runtime.system_event_bus import SystemEventBus
from orca.runtime.system_runtime import SystemRuntime
from orca.runtime.teachpoint_service import ITeachpointService, TeachpointService
from orca.variables.variable_store import IVariableService, VariableService, VariableStore
from orca.sdk.build import SystemBuild
from orca.state.ops_store import IOpsHistoryStore
from orca.system.system_interface import ISystem

_logger = logging.getLogger(__name__)

START_TIMEOUT_SECONDS = 120.0
STALL_CHECK_INTERVAL_SECONDS = 10.0
# A verdict can land just after the first tick that saw the state, so N ticks only guarantee N-1 intervals.
STALL_REQUIRED_STABLE_TICKS = 3
# Bounded on its own, so tearing down a runtime that never started cannot hang either.
_ABANDON_TIMEOUT_SECONDS = 15.0

CurrentRuntime = Callable[[], ISystemRuntime | None]


@dataclass(frozen=True)
class RuntimeSettings:
    """How a server runs its runtime. Each server builds one at startup from its own config."""

    default_sim_speedup: float | None
    start_timeout: float
    # None turns the stall detector off.
    stall_check_interval: float | None
    stall_required_stable_ticks: int


def runtime_settings(default_sim_speedup: float | None) -> RuntimeSettings:
    """The settings both servers run with."""
    return RuntimeSettings(
        default_sim_speedup=default_sim_speedup,
        start_timeout=START_TIMEOUT_SECONDS,
        stall_check_interval=STALL_CHECK_INTERVAL_SECONDS,
        stall_required_stable_ticks=STALL_REQUIRED_STABLE_TICKS,
    )


class IActivityServices(Protocol):
    """What the lab does: plates, incidents, run records, the operation record and each device's sim or live setting."""

    @property
    def labware(self) -> ILabwareService: ...

    @property
    def incidents(self) -> IIncidentService: ...

    @property
    def execution_records(self) -> IExecutionRecordService: ...

    @property
    def ops_history(self) -> IOpsHistoryStore: ...

    @property
    def device_modes(self) -> IDeviceModes: ...

    @property
    def unsaved_records(self) -> IUnsavedRecordReporter: ...

    @property
    def event_saves(self) -> EventSaves: ...


@dataclass(frozen=True)
class ActivityServices:
    labware: ILabwareService
    incidents: IIncidentService
    execution_records: IExecutionRecordService
    ops_history: IOpsHistoryStore
    device_modes: IDeviceModes
    unsaved_records: IUnsavedRecordReporter
    event_saves: EventSaves = field(default_factory=EventSaves)


@dataclass(frozen=True)
class ActivityStores:
    """The stores of the database a server chose for what the lab does."""

    labware: ILabwareStore
    incidents: IIncidentStore
    execution_records: IExecutionRecordStore
    ops_history: IOpsHistoryStore
    device_modes: IDeviceModes


class IServerActivity(IActivityServices, Protocol):
    """The activity services, with the background writes a server starts and stops."""

    async def create_schema(self) -> None: ...

    def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def aclose(self) -> None: ...


class _ServerActivity:
    def __init__(self, stores: ActivityStores) -> None:
        self._labware = LabwareService(stores.labware)
        self._incidents = IncidentService(stores.incidents)
        self._unsaved_records = UnsavedRecordReporter(self._incidents.record)
        self._incidents.report_unsaved_to(self._unsaved_records)
        self._execution_records = ExecutionRecordService(stores.execution_records, self._unsaved_records)
        self._ops_history = stores.ops_history
        self._device_modes = stores.device_modes
        self._event_saves = EventSaves()

    @property
    def unsaved_records(self) -> IUnsavedRecordReporter:
        return self._unsaved_records

    @property
    def event_saves(self) -> EventSaves:
        return self._event_saves

    @property
    def labware(self) -> ILabwareService:
        return self._labware

    @property
    def incidents(self) -> IIncidentService:
        return self._incidents

    @property
    def execution_records(self) -> IExecutionRecordService:
        return self._execution_records

    @property
    def ops_history(self) -> IOpsHistoryStore:
        return self._ops_history

    @property
    def device_modes(self) -> IDeviceModes:
        return self._device_modes

    async def create_schema(self) -> None:
        await self._incidents.ensure_schema()
        await self._execution_records.ensure_schema()

    def start(self) -> None:
        self._incidents.start_drain_task()
        self._execution_records.start_drain_task()

    async def stop(self) -> None:
        # Run records drain first: a status they cannot save files an incident, which must still be saved.
        try:
            await self._execution_records.stop_drain_task()
        finally:
            await self._incidents.stop_drain_task()

    async def aclose(self) -> None:
        try:
            await self.stop()
        finally:
            try:
                await self._execution_records.aclose()
            finally:
                await self._incidents.aclose()


def build_activity(stores: ActivityStores) -> IServerActivity:
    """The activity services over the server's own stores. Call `start` once the loop runs."""
    return _ServerActivity(stores)


class CheckRunServices:
    """One check run's plates, incidents and operation record, over the stores the server made for it."""

    def __init__(
        self, *, labware: ILabwareStore, incidents: IIncidentStore, ops_history: IOpsHistoryStore,
        release: Callable[[], Awaitable[None]],
    ) -> None:
        self._labware = LabwareService(labware)
        self._incidents = IncidentService(incidents)
        self._incidents.start_drain_task()
        self._ops_history = ops_history
        self._release = release

    @property
    def labware(self) -> ILabwareService:
        return self._labware

    @property
    def incidents(self) -> IIncidentService:
        return self._incidents

    @property
    def ops_history(self) -> IOpsHistoryStore:
        return self._ops_history

    async def aclose(self) -> None:
        await self._incidents.aclose()
        await self._release()


class ConfiguredPlugins:
    """The plugins a server's config declares.

    Each is a sink of the server's event hub from `start` to `stop`, so it hears every event the server sends,
    with or without a runtime. Each runtime the server builds lists them.
    """

    def __init__(self, plugins: Sequence[AsyncOffloadingPlugin] = ()) -> None:
        self._plugins = tuple(plugins)
        self._events: IServerEvents | None = None

    @property
    def plugins(self) -> Sequence[AsyncOffloadingPlugin]:
        return self._plugins

    def start(self, events: IServerEvents) -> None:
        for plugin in self._plugins:
            plugin.start()
            events.add_sink(plugin)
        self._events = events

    async def stop(self) -> None:
        events, self._events = self._events, None
        for plugin in self._plugins:
            if events is not None:
                events.remove_sink(plugin)
            await plugin.stop()


def load_plugins(configs: Sequence[PluginConfig]) -> ConfiguredPlugins:
    """One plugin per config entry. A plugin is enabled by config, never by submitted code."""
    return ConfiguredPlugins([
        WebhookPlugin(
            url=config.url,
            headers=config.headers,
            event_filter=config.event_filter,
            timeout=config.timeout,
            max_retries=config.max_retries,
            backoff_base=config.backoff_base,
            queue_size=config.queue_size,
        )
        for config in configs
    ])


def start_log_store_writers(
    audit: IAuditStore, events: IEventStore, unsaved_records: IUnsavedRecordReporter, event_saves: EventSaves,
) -> tuple[LogStoreWriter, ...]:
    """Store every logged audit row and event in the server's database until each writer is stopped.

    A run's end waits on ``event_saves`` for the event writer, so its events are saved before it is recorded ended.
    """
    loop = asyncio.get_running_loop()
    event_writer = EventWriter(events, loop, unsaved_records)
    writers: tuple[LogStoreWriter, ...] = (AuditWriter(audit, loop, unsaved_records), event_writer)
    for writer in writers:
        writer.start()
    event_saves.add(event_writer)
    return writers


@dataclass(frozen=True)
class DeviceConnections:
    """Which device bridges are connected, as the runtime's gateway and registry read it, and the gateway that drives them."""

    gateway_registry: IGatewayRegistry
    connection_source: IDeviceConnectionSource
    gateway: DeviceGateway


class _GatewayParts(TypedDict):
    tracker: DeviceConnectionTracker
    connections: IClientChannel
    events: ConnectionEventBus
    controller: DeviceController
    assignments: DeviceAssignments


def build_device_gateway(events: IEventEmitter, default_sim_speedup: float | None) -> ServerDeviceGateway:
    """The concrete gateway, sending each device command's events to `events`. Each server calls this once, in its startup code.

    `default_sim_speedup` paces a simulated device's command that no run paces; None waits for nothing.
    """
    sockets = ConnectionManager()
    parts = _gateway_parts(
        DeviceConnectionTracker(), sockets, events, serves_check_runs=False, default_sim_speedup=default_sim_speedup,
    )
    return ServerDeviceGateway(**parts, sockets=sockets)


def _gateway_parts(
    tracker: DeviceConnectionTracker, connections: IClientChannel, events: IEventEmitter, *, serves_check_runs: bool,
    default_sim_speedup: float | None,
) -> _GatewayParts:
    return _GatewayParts(
        tracker=tracker,
        connections=connections,
        events=ConnectionEventBus(),
        controller=DeviceController(
            tracker=tracker, connections=connections,
            disconnect_grace=DisconnectGrace(), command_clock=DeviceCommandClock(),
            events=events, serves_check_runs=serves_check_runs, default_sim_speedup=default_sim_speedup,
        ),
        assignments=DeviceAssignments(connections),
    )


def connections_over(gateway: DeviceGateway) -> DeviceConnections:
    """What the runtime reads of `gateway`'s connected bridges."""
    source = DeviceConnectionSource(gateway.tracker)
    return DeviceConnections(gateway_registry=source, connection_source=source, gateway=gateway)


@dataclass(frozen=True)
class CheckRunDevices:
    """A check run's devices: its own gateway over an orca-client inside Orca, and the factory that wires devices to it."""

    connections: DeviceConnections
    device_factory: IDeviceDriverProvider
    client: InProcessClient


def build_check_run_devices(
    setup: IRuntimeStoreFactory, current_system: Callable[[], HasSystem | None], events: IEventEmitter,
    client_over: Callable[[DeviceConnectionTracker], InProcessClient] = InProcessClient,
    expected_interfaces: Callable[[IDeviceSetupStore], Callable[[str, str, bool], frozenset[str]]] = simulator_interfaces_from_setups,
) -> CheckRunDevices:
    """The same controller and remote drivers a sim run uses, with the simulators run in this process.

    By default every device is expected to implement what its simulator does, as a check run runs nothing else.
    """
    tracker = DeviceConnectionTracker()
    client = client_over(tracker)
    # A check run is never paced.
    gateway = DeviceGateway(**_gateway_parts(tracker, client, events, serves_check_runs=True, default_sim_speedup=None))
    client.answer_to(gateway)
    return CheckRunDevices(
        connections=connections_over(gateway),
        device_factory=_remote_device_factory(
            gateway, current_system, lambda: WorkflowRunMode.CHECK_RUN, expected_interfaces(setup.device_setups()),
        ),
        client=client,
    )


def _remote_device_factory(
    gateway: DeviceGateway, current_system: Callable[[], HasSystem | None], base_mode: BaseMode,
    profile_source: Callable[[str, str, bool], frozenset[str]],
) -> RemoteDeviceFactory:
    return RemoteDeviceFactory(
        controller=gateway.controller,
        mode_resolver=system_mode_resolver(current_system, base_mode=base_mode),
        profile_source=profile_source,
    )


def in_memory_labware_service() -> ILabwareService:
    """A labware record held in memory, for a system built outside a server, such as in a test."""
    return LabwareService(InMemoryLabwareStore())


class UnsavedPositionsInMemory:
    """The positions a system built outside a server could not save after their caller was cancelled."""

    def __init__(self) -> None:
        self.reported: list[tuple[str, str]] = []

    def report(self, labware: LabwareInstance, error: PositionNotSavedError) -> None:
        self.reported.append((labware.id, str(error)))


def in_memory_unsaved_positions() -> UnsavedPositionsInMemory:
    """Where a system built outside a server, such as in a test, keeps the positions it could not save."""
    return UnsavedPositionsInMemory()


# Builds the package over the bus the runtime will share and the labware record its placer saves to.
BuildStep = Callable[[IEventBus, ILabwareService, IUnsavedPositions], Awaitable[SystemBuild]]


@dataclass(frozen=True)
class StartedRuntime:
    build: SystemBuild
    runtime: ISystemRuntime


class PackageSetupStoreFactory:
    """Setup stores backed by the JSON files of one deployment package.

    `root` is the package folder: `teachpoints/<device>.json`,
    `deck_layouts/<device>.json`, `access_configs.json`, `move_defaults.json`,
    `grip_profiles.json`, `system_config.json`, `global_variables.json`, `labware/` and `profiles/`.
    Every read goes to the file.
    """

    def __init__(self, root: Path, *, default_client: str) -> None:
        self._root = Path(root)
        self._default_client = default_client
        self._access_configs = AccessConfigService(
            AccessConfigFileStore(self._root / "access_configs.json"),
        )
        self._move_defaults = MoveDefaultsService(
            MoveDefaultsFileStore(self._root / "move_defaults.json"),
        )
        self._global_variables = GlobalVariableService(GlobalVariableFileStore(self._root / GLOBAL_VARIABLES_FILE))
        self._grip_profiles = GripProfileService(
            GripProfileFileStore(self._root / "grip_profiles.json"),
        )
        self._profiles = DeploymentProfileService(
            FileDeploymentProfileStore(self._root / "profiles"),
        )
        self._device_setups = DeviceSetupService(DeviceSetupFileStore(self._root / "devices", default_client))
        self._teachpoints: dict[str, ITeachpointService] = {}
        self._deck_layouts: dict[str, IDeckLayoutService] = {}
        self._catalog_service = LabwareCatalogService(
            LabwareCatalogFileStore(self._root / "labware"),
        )

    @property
    def root(self) -> Path:
        return self._root

    def device_setups(self) -> IDeviceSetupService:
        return self._device_setups

    def teachpoints(self, device_id: str) -> ITeachpointService:
        if device_id not in self._teachpoints:
            self._teachpoints[device_id] = TeachpointService(TeachpointFileStore(
                self._root / "teachpoints" / f"{device_id}.json",
                self._root / "access_configs.json",
            ))
        return self._teachpoints[device_id]

    def deck_layouts(self, device_id: str) -> IDeckLayoutService:
        if device_id not in self._deck_layouts:
            self._deck_layouts[device_id] = DeckLayoutService(DeckLayoutFileStore(
                self._root / "deck_layouts" / f"{device_id}.json",
            ))
        return self._deck_layouts[device_id]

    def access_configs(self) -> IAccessConfigService:
        return self._access_configs

    def move_defaults(self) -> IMoveDefaultsService:
        return self._move_defaults

    def grip_profiles(self) -> IGripProfileService:
        return self._grip_profiles

    def profiles(self) -> IDeploymentProfileService:
        return self._profiles

    def system_config(self) -> OrcaConfig:
        return read_system_config(self._root / SYSTEM_CONFIG_FILE)

    def global_variables(self) -> IGlobalVariableService:
        return self._global_variables

    def variable_store(self) -> IVariableService:
        return VariableService(VariableStore())

    def labware_catalog_store(self) -> ILabwareCatalogService:
        return self._catalog_service

    def labware_catalog(self) -> ILabwareCatalog:
        return StoreBackedLabwareCatalog(self._catalog_service)


def deployment_registries_over(setup: IRuntimeStoreFactory) -> IDeploymentRegistries:
    """The operator facades over `setup`, so the runtime and the operator surfaces read the same stores."""
    return DeploymentRegistries(
        labware_catalog=setup.labware_catalog_store(),
        access_configs=AccessConfigFacade(setup.access_configs()),
        move_defaults=MoveDefaultsFacade(setup.move_defaults()),
        grip_profiles=GripProfileFacade(setup.grip_profiles()),
        profiles=DeploymentProfileFacade(setup.profiles()),
        device_setups=DeviceSetupFacade(setup.device_setups()),
    )


@dataclass(frozen=True)
class ServerParts:
    """What a server builds once over its setup stores and gateway, and passes to everything that needs it."""

    setup: IRuntimeStoreFactory
    registries: IDeploymentRegistries
    connections: DeviceConnections
    device_factory: IDeviceDriverProvider


def build_server_parts(
    setup: IRuntimeStoreFactory, gateway: DeviceGateway, current_runtime: CurrentRuntime,
) -> ServerParts:
    """The registries over `setup`, the runtime's view of `gateway`, and the factory that makes every device go through it.

    `current_runtime` is a provider, because a rebuild swaps the runtime.
    """
    return ServerParts(
        setup=setup,
        registries=deployment_registries_over(setup),
        connections=connections_over(gateway),
        device_factory=_remote_device_factory(
            gateway, current_runtime, _operator_base_of(current_runtime), interfaces_from_setups(setup.device_setups()),
        ),
    )


def _operator_base_of(current_runtime: CurrentRuntime) -> BaseMode:
    """The current runtime's operator base: an operator command goes in the active run's mode."""
    def base_mode() -> WorkflowRunMode:
        runtime = current_runtime()
        return runtime.operator_base_mode() if runtime is not None else OUT_OF_RUN_BASE
    return base_mode


def operator_device_commands(gateway: DeviceGateway, current_runtime: CurrentRuntime) -> IDeviceCommandRunner:
    """The one operator path to this server's devices."""
    return OperatorDeviceCommands(gateway, current_runtime)


class RuntimeStartTimeoutError(RuntimeError):
    """The package build and the runtime's start did not finish within the start timeout."""


def build_runtime(
    system: ISystem,
    *,
    setup: IRuntimeStoreFactory,
    activity: IActivityServices,
    connections: DeviceConnections,
    event_bus: IEventBus,
    settings: RuntimeSettings,
) -> ISystemRuntime:
    """The one place a SystemRuntime is constructed. It records operations in the system's own store."""
    # The registry and the devices are built before the runtime they read the active run from.
    built: list[ISystemRuntime] = []

    def base_mode() -> WorkflowRunMode:
        return built[0].operator_base_mode() if built else OUT_OF_RUN_BASE

    for resource in system.resources:
        if isinstance(resource, IModeAware):
            resource.use_device_modes(activity.device_modes, base_mode)
    topology = SystemTopologyRegistry(system)

    runtime = SystemRuntime(
        system,
        incident_service=activity.incidents,
        execution_record_service=activity.execution_records,
        unsaved_records=activity.unsaved_records,
        event_saves=activity.event_saves,
        access_config_service=setup.access_configs(),
        move_defaults_service=setup.move_defaults(),
        grip_profile_service=setup.grip_profiles(),
        profile_store=setup.profiles(),
        labware_catalog_store=setup.labware_catalog_store(),
        gateway_registry=connections.gateway_registry,
        connection_source=connections.connection_source,
        device_gateway=connections.gateway,
        event_bus=event_bus,
        system_event_bus=SystemEventBus(),
        topology_registry=topology,
        device_registry=DeviceRegistryImpl(topology, connections.connection_source, system, base_mode),
        variables=VariableFacade(system.variable_store, setup.global_variables()),
        teachpoints=TeachpointFacade(system),
        deck_layouts=DeckLayoutFacade(system),
        ops_history=OpsHistoryFacade(system.ops_history.store),
        device_modes=DeviceModeFacade(activity.device_modes, setup.device_setups(), system),
        default_sim_speedup=settings.default_sim_speedup,
        stall_check_interval=settings.stall_check_interval,
        stall_required_stable_ticks=settings.stall_required_stable_ticks,
    )
    built.append(runtime)
    return runtime


async def start_runtime(
    build_step: BuildStep,
    *,
    setup: IRuntimeStoreFactory,
    activity: IActivityServices,
    connections: DeviceConnections,
    sinks: Sequence[IEventSink],
    labware_events: IEventEmitter,
    plugins: Sequence[OrcaPlugin],
    settings: RuntimeSettings,
    after_reload: bool = False,
) -> StartedRuntime:
    """Run `build_step`, build the runtime over its result, register every sink and plugin, and start it.

    `after_reload` says the previous runtime was shut down in this process just before, so nothing went unwatched.

    `plugins` are the server's configured plugins: the runtime lists them, and they hear events on the server's hub.
    `labware_events` takes the LABWARE event of each write to the runtime's labware record.

    `settings.start_timeout` covers all of it. A start that fails or times out shuts
    the runtime down before the error is raised.
    """
    built: list[ISystemRuntime] = []

    async def _build_and_start() -> StartedRuntime:
        # Announced, so every write to the labware record tells a reader that its copy is stale.
        build = await build_step(
            EventBus(), AnnouncedLabwareService(activity.labware, labware_events),
            UnsavedPositions(activity.unsaved_records),
        )
        if build.system.ops_history.store is not activity.ops_history:
            raise ValueError(
                "the system was built with a different operation store than the server chose; "
                "pass activity.ops_history to the build",
            )
        runtime = build_runtime(
            build.system,
            setup=setup,
            activity=activity,
            connections=connections,
            event_bus=build.event_bus,
            settings=settings,
        )
        built.append(runtime)
        # Before start, so the events start sends reach every sink.
        for sink in sinks:
            runtime.register_sink(sink)
        for plugin in plugins:
            # Listed only: a configured plugin hears every event on the server's event hub.
            runtime.register_plugin(plugin, subscribe=False)
        await runtime.start(after_reload=after_reload)
        return StartedRuntime(build=build, runtime=runtime)

    try:
        return await asyncio.wait_for(_build_and_start(), timeout=settings.start_timeout)
    except asyncio.TimeoutError as exc:
        await _abandon(built)
        raise RuntimeStartTimeoutError(
            f"the package did not build and start within {settings.start_timeout}s. Check the server logs.",
        ) from exc
    except BaseException:
        await _abandon(built)
        raise


async def _abandon(built: list[ISystemRuntime]) -> None:
    # Never raises: the start error is the one the operator needs.
    if not built:
        return
    try:
        await asyncio.wait_for(built[0].shutdown(confirm=True), timeout=_ABANDON_TIMEOUT_SECONDS)
    except BaseException:
        _logger.exception(
            "a runtime whose start failed did not shut down; its listeners and tasks may still be live",
        )
