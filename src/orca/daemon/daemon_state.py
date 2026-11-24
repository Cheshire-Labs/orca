"""Everything the daemon chose at startup, and the holder of its mounted system."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from orca.api.interfaces import IEventFeed
from orca.gateway.device_command_runner import IDeviceCommandRunner
from orca.operations.audit import IAuditLog
from orca.operations.events import IEventLog
from orca.runtime.activity_database import IActivityStores
from orca.runtime.deployment_registries import IDeploymentRegistries
from orca.events.emitter import IServerEvents
from orca.runtime.runtime_holder import RuntimeHolder
from orca.gateway.device_gateway import ServerDeviceGateway
from orca.runtime.startup import ConfiguredPlugins, DeviceConnections
from orca.runtime.store_factory import IRuntimeStoreFactory

OnExit = Callable[[], Awaitable[None]]


@dataclass
class DaemonState:
    package_root: Path
    setup: IRuntimeStoreFactory
    activity: IActivityStores
    registries: IDeploymentRegistries
    audit_log: IAuditLog
    event_log: IEventLog
    connections: DeviceConnections
    gateway: ServerDeviceGateway
    event_sink: IEventFeed
    # Every event this daemon sends, from a mounted runtime or not, reaches its sinks through this.
    events: IServerEvents
    device_commands: IDeviceCommandRunner
    plugins: ConfiguredPlugins
    on_exit: OnExit | None
    holder: RuntimeHolder
