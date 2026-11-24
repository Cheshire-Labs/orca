"""Device-mode settings for tests."""

from orca.resource_models.resources import IModeAware, IResource
from orca.runtime.registries.device_link import mark_agent_held
from orca.modes.device_modes import DeviceMode
from orca.system.system_interface import ISystem


class InMemoryDeviceModes:
    def __init__(self, modes: dict[str, DeviceMode] | None = None) -> None:
        self._modes = dict(modes or {})

    async def mode_of(self, device_name: str) -> DeviceMode:
        return self._modes.get(device_name, DeviceMode.SIM)

    async def set(self, device_name: str, mode: DeviceMode) -> None:
        self._modes[device_name] = mode


def set_to(resource: IModeAware, name: str, mode: DeviceMode) -> None:
    resource.use_device_modes(InMemoryDeviceModes({name: mode}))


async def modes_the_devices_were_built_with(system: ISystem) -> InMemoryDeviceModes:
    """The settings a test gave its devices, so building the runtime keeps them."""
    return InMemoryDeviceModes(
        {r.name: await r.setting() for r in system.resources if isinstance(r, IModeAware)},
    )


def run_by_an_orca_client(*resources: IResource) -> None:
    """Mark each device's driver as reached through an orca-client, as every device a server builds is."""
    for resource in resources:
        driver = getattr(resource, "driver", None)
        if driver is not None:
            mark_agent_held(driver)
