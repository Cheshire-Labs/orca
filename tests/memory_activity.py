"""A test runtime on in-memory activity services, closed after each test."""

import asyncio
from dataclasses import dataclass
from typing import TypedDict

from typing_extensions import Unpack

from orca.runtime.event_saves import EventSaves
from orca.config import OrcaConfig
from orca.runtime.global_variable_service import IGlobalVariableService
from orca.gateway.device_gateway import DeviceGateway
from orca.daemon.app import InjectedSystem
from orca.events.event_bus import EventBus
from orca.modes.device_modes import IDeviceModes
from tests.device_modes import modes_the_devices_were_built_with
from orca.events.event_bus_interface import IEventBus
from orca.runtime.audit_store import IAuditStore
from orca.runtime.sqlite_audit_store import SqliteAuditStore
from orca.runtime.access_config_service import AccessConfigService, IAccessConfigService
from orca.runtime.db import create_memory_engine
from orca.runtime.execution_record_service import ExecutionRecordService, IExecutionRecordService
from orca.runtime.grip_profile_service import GripProfileService, IGripProfileService
from orca.runtime.incident_service import IIncidentService, IncidentService
from orca.runtime.deck_layout_service import IDeckLayoutService
from orca.runtime.deployment_profile_service import IDeploymentProfileService
from orca.runtime.device_setup_service import IDeviceSetupService
from orca.resource_models.labware_catalog_protocol import ILabwareCatalog
from orca.runtime.teachpoint_service import ITeachpointService
from orca.resource_models.labware_service_interface import ILabwareService
from orca.events.emitter import IEventEmitter
from orca.runtime.startup import UnsavedPositionsInMemory, in_memory_unsaved_positions
from orca.runtime.labware_service import AnnouncedLabwareService, LabwareService
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.labware_catalog_service import ILabwareCatalogService, LabwareCatalogService
from orca.runtime.move_defaults_service import IMoveDefaultsService, MoveDefaultsService
from orca.runtime.registry_protocols import IDeviceConnectionSource, IGatewayRegistry
from orca.runtime.sqlite_execution_record_store import SqliteExecutionRecordStore
from orca.runtime.sqlite_incident_store import SqliteIncidentStore
from orca.runtime.startup import (
    START_TIMEOUT_SECONDS,
    STALL_CHECK_INTERVAL_SECONDS,
    STALL_REQUIRED_STABLE_TICKS,
    ActivityServices,
    DeviceConnections,
    RuntimeSettings,
    build_runtime,
)
from orca.runtime.store_factory import IRuntimeStoreFactory, default_package_root
from orca.runtime.unsaved_record import IUnsavedRecordReporter, UnsavedRecordReporter
from orca.runtime.startup import PackageSetupStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.state.ops_store import IOpsHistoryStore
from orca.variables.variable_store import IVariableService, VariableService, VariableStore
from orca.system.system_interface import ISystem
from orca.runtime.event_store import IEventStore
from tests.ephemeral_ops_store import EphemeralEventStore, EphemeralOpsHistoryStore
from cheshire_drivers.device_setup import BUILTIN_CLIENT
from tests.real_device_path import the_real_device_path
from tests.gateway_parts import served


class RuntimeOptions(TypedDict, total=False):
    event_bus: IEventBus | None
    stall_check_interval: float | None
    stall_required_stable_ticks: int
    default_sim_speedup: float | None


class _SetupWith:
    """The package folder's setup, with the services a test passes in place of its own."""

    def __init__(
        self,
        base: IRuntimeStoreFactory,
        *,
        access_config_service: AccessConfigService | None,
        move_defaults_service: MoveDefaultsService | None,
        grip_profile_service: GripProfileService | None,
        profile_store: IDeploymentProfileService | None,
        labware_catalog_store: LabwareCatalogService | None,
    ) -> None:
        self._base = base
        self._access_configs = access_config_service
        self._move_defaults = move_defaults_service
        self._grip_profiles = grip_profile_service
        self._profiles = profile_store
        self._labware_catalog_store = labware_catalog_store

    def teachpoints(self, device_id: str) -> ITeachpointService:
        return self._base.teachpoints(device_id)

    def deck_layouts(self, device_id: str) -> IDeckLayoutService:
        return self._base.deck_layouts(device_id)

    def device_setups(self) -> IDeviceSetupService:
        return self._base.device_setups()

    def access_configs(self) -> IAccessConfigService:
        return self._access_configs or self._base.access_configs()

    def move_defaults(self) -> IMoveDefaultsService:
        return self._move_defaults or self._base.move_defaults()

    def grip_profiles(self) -> IGripProfileService:
        return self._grip_profiles or self._base.grip_profiles()

    def profiles(self) -> IDeploymentProfileService:
        return self._profiles or self._base.profiles()

    def system_config(self) -> OrcaConfig:
        return self._base.system_config()

    def global_variables(self) -> IGlobalVariableService:
        return self._base.global_variables()

    def variable_store(self) -> IVariableService:
        return self._base.variable_store()

    def labware_catalog_store(self) -> ILabwareCatalogService:
        return self._labware_catalog_store or self._base.labware_catalog_store()

    def labware_catalog(self) -> ILabwareCatalog:
        return self._base.labware_catalog()


def memory_labware_service() -> ILabwareService:
    """The labware record a system a test builds saves its positions to."""
    return LabwareService(InMemoryLabwareStore())


def memory_unsaved_positions() -> UnsavedPositionsInMemory:
    """Where a system a test builds reports a position it could not save."""
    return in_memory_unsaved_positions()


def announced_labware_service(events: IEventEmitter) -> ILabwareService:
    """As `memory_labware_service`, sending a LABWARE event to `events` after each write, as a server's does."""
    return AnnouncedLabwareService(memory_labware_service(), events)


def memory_ops_store() -> IOpsHistoryStore:
    """An operation record in a temporary folder, for a system a test builds."""
    return EphemeralOpsHistoryStore()


def memory_variable_store() -> IVariableService:
    return VariableService(VariableStore())


class _OpenServices:
    def __init__(self) -> None:
        self._services: list[IncidentService | ExecutionRecordService] = []

    async def incident_service(self) -> IncidentService:
        service = IncidentService(SqliteIncidentStore(create_memory_engine()))
        await self._open(service)
        return service

    async def execution_record_service(self, unsaved_records: IUnsavedRecordReporter) -> ExecutionRecordService:
        service = ExecutionRecordService(SqliteExecutionRecordStore(create_memory_engine()), unsaved_records)
        await self._open(service)
        return service

    async def _open(self, service: IncidentService | ExecutionRecordService) -> None:
        await service.ensure_schema()
        service.start_drain_task()
        self._services.append(service)

    async def aclose(self) -> None:
        for service in self._services:
            await service.aclose()
        self._services.clear()


# Per loop: a module fixture that runs its own asyncio.run opens services on a loop
# that has ended, and cancelled its drains, before the per-test close runs.
_open_by_loop: dict[asyncio.AbstractEventLoop, _OpenServices] = {}


def _open_services() -> _OpenServices:
    return _open_by_loop.setdefault(asyncio.get_running_loop(), _OpenServices())


async def memory_runtime(
    system: ISystem,
    *,
    incident_service: IncidentService | None = None,
    execution_record_service: ExecutionRecordService | None = None,
    access_config_service: AccessConfigService | None = None,
    move_defaults_service: MoveDefaultsService | None = None,
    grip_profile_service: GripProfileService | None = None,
    profile_store: IDeploymentProfileService | None = None,
    labware_catalog_store: LabwareCatalogService | None = None,
    gateway_registry: IGatewayRegistry | None = None,
    connection_source: IDeviceConnectionSource | None = None,
    gateway: DeviceGateway | None = None,
    **options: Unpack[RuntimeOptions],
) -> SystemRuntime:
    """A SystemRuntime built by `build_runtime`, not started; each service not passed is a fresh in-memory one."""
    injected = await memory_daemon_system(
        system,
        incident_service=incident_service,
        execution_record_service=execution_record_service,
        access_config_service=access_config_service,
        move_defaults_service=move_defaults_service,
        grip_profile_service=grip_profile_service,
        profile_store=profile_store,
        labware_catalog_store=labware_catalog_store,
        gateway_registry=gateway_registry,
        connection_source=connection_source,
        gateway=gateway,
        **options,
    )
    return injected.runtime


@dataclass(frozen=True)
class MemoryActivity:
    """Test activity services plus audit and event stores; `_OpenServices` opens and closes them."""

    labware: ILabwareService
    incidents: IIncidentService
    execution_records: IExecutionRecordService
    ops_history: IOpsHistoryStore
    device_modes: IDeviceModes
    unsaved_records: IUnsavedRecordReporter
    audit: IAuditStore
    events: IEventStore
    event_saves: EventSaves

    async def open(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


async def memory_daemon_system(
    system: ISystem,
    *,
    incident_service: IncidentService | None = None,
    execution_record_service: ExecutionRecordService | None = None,
    access_config_service: AccessConfigService | None = None,
    move_defaults_service: MoveDefaultsService | None = None,
    grip_profile_service: GripProfileService | None = None,
    profile_store: IDeploymentProfileService | None = None,
    labware_catalog_store: LabwareCatalogService | None = None,
    gateway_registry: IGatewayRegistry | None = None,
    connection_source: IDeviceConnectionSource | None = None,
    gateway: DeviceGateway | None = None,
    **options: Unpack[RuntimeOptions],
) -> InjectedSystem:
    """A runtime, not started, with the setup and activity stores it was built over, for `create_app(injected=...)`."""
    services = await memory_activity_services(
        system,
        incident_service=incident_service,
        execution_record_service=execution_record_service,
    )
    activity = MemoryActivity(
        labware=services.labware,
        incidents=services.incidents,
        execution_records=services.execution_records,
        ops_history=services.ops_history,
        device_modes=services.device_modes,
        unsaved_records=services.unsaved_records,
        event_saves=services.event_saves,
        audit=SqliteAuditStore(create_memory_engine()),
        events=EphemeralEventStore(),
    )
    setup = _SetupWith(
        PackageSetupStoreFactory(default_package_root(), default_client=BUILTIN_CLIENT),
        access_config_service=access_config_service,
        move_defaults_service=move_defaults_service,
        grip_profile_service=grip_profile_service,
        profile_store=profile_store,
        labware_catalog_store=labware_catalog_store,
    )
    path = the_real_device_path()
    connections = memory_connections(gateway_registry, connection_source, gateway)
    runtime = build_runtime(
        system,
        setup=setup,
        activity=activity,
        connections=connections,
        event_bus=options.get("event_bus") or EventBus(),
        settings=RuntimeSettings(
            default_sim_speedup=options.get("default_sim_speedup"),
            start_timeout=START_TIMEOUT_SECONDS,
            stall_check_interval=options.get("stall_check_interval", STALL_CHECK_INTERVAL_SECONDS),
            stall_required_stable_ticks=options.get("stall_required_stable_ticks", STALL_REQUIRED_STABLE_TICKS),
        ),
    )
    if connections.gateway is path.gateway:
        await path.serve(runtime)
    return InjectedSystem(
        runtime=runtime, setup=setup, activity=activity, gateway=served(connections.gateway),
    )


async def memory_activity_services(
    system: ISystem,
    *,
    incident_service: IncidentService | None = None,
    execution_record_service: ExecutionRecordService | None = None,
) -> ActivityServices:
    """The system's own operation record and labware record, plus in-memory services for what is not passed."""
    if incident_service is None:
        incident_service = await _open_services().incident_service()
    unsaved_records = UnsavedRecordReporter(incident_service.record)
    incident_service.report_unsaved_to(unsaved_records)
    if execution_record_service is None:
        execution_record_service = await _open_services().execution_record_service(unsaved_records)
    return ActivityServices(
        labware=system.labware_service,
        incidents=incident_service,
        execution_records=execution_record_service,
        ops_history=system.ops_history.store,
        device_modes=await modes_the_devices_were_built_with(system),
        unsaved_records=unsaved_records,
    )


def memory_connections(
    gateway_registry: IGatewayRegistry | None = None,
    connection_source: IDeviceConnectionSource | None = None,
    gateway: DeviceGateway | None = None,
) -> DeviceConnections:
    """The real device path's connections, each replaced by the one the test passes."""
    path = the_real_device_path().connections
    return DeviceConnections(
        gateway_registry=gateway_registry or path.gateway_registry,
        connection_source=connection_source or path.connection_source,
        gateway=gateway or path.gateway,
    )


async def close_memory_activity() -> None:
    """Close what this loop opened; call it before a loop of your own ends."""
    services = _open_by_loop.pop(asyncio.get_running_loop(), None)
    for loop in [loop for loop in _open_by_loop if loop.is_closed()]:
        del _open_by_loop[loop]
    if services is not None:
        await services.aclose()
