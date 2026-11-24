"""Device-registry implementations for the orca-core SystemRuntime.

`SystemTopologyRegistry` walks the System resource registry to enumerate
devices declared by `topology.py`.

`DeviceRegistryImpl` composes a topology source plus a connection source
into the unified two-card view exposed at `runtime.devices`. A hosted
deployment injects DB-backed `IGatewayRegistry` and
`IDeviceConnectionSource` implementations sourced
from the in-memory `DeviceConnectionTracker` plus the persisted
`RegisteredDevice` row.

`DeviceLinkReader` holds the one rule for "is this device linked, and is it
brought up", shared by the registry and by both snapshot facades so no two read
surfaces can answer differently.
"""

from orca.runtime.registries.device_registry import DeviceRegistryImpl
from orca.runtime.registries.topology_registry import SystemTopologyRegistry

__all__ = [
    "DeviceRegistryImpl",
    "SystemTopologyRegistry",
]
