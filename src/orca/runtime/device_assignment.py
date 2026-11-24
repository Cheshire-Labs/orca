"""Which orca-client runs each device: the package's device setups matched against its topology."""

from collections.abc import Callable, Iterable

from cheshire_drivers.device_setup import DeviceSetup
from cheshire_drivers.driver_lookup import (
    SIM_DRIVER_CLASS_BY_KIND,
    check_device_setup,
    driver_interfaces,
    sim_driver_interfaces,
)
from cheshire_drivers.gateway_protocol import AssignedDevice
from pydantic import ValidationError

from orca.resource_models.devices import Device
from orca.resource_models.site_rack import SiteRack
from orca.resource_models.transporter import Transporter
from orca.resource_models.store_interfaces import IDeviceSetupStore

def _refuse_rack_setup(rack: SiteRack) -> None:
    raise ValueError(f"Rack {rack.name!r} has no driver, so it takes no setup file; remove devices/{rack.name}.json.")


def _with_client(name: str, setup: DeviceSetup, default_client: str) -> DeviceSetup:
    """A setup naming no client goes to the default client, and is checked again as that client's."""
    if setup.client is not None:
        return setup
    try:
        return DeviceSetup.model_validate({**setup.model_dump(exclude_unset=True), "client": default_client})
    except ValidationError as refused:
        raise ValueError(f"devices/{name}.json names no client, so it goes to {default_client!r}, which cannot run it: {refused}") from refused


def assign_devices(
    resources: Iterable[Device | Transporter | SiteRack], store: IDeviceSetupStore,
) -> list[AssignedDevice]:
    """One assignment per device with commands. Refuses a setup the topology or the driver cannot honour."""
    resources = list(resources)
    devices = [resource for resource in resources if isinstance(resource, Device | Transporter)]
    racks = {resource.name: resource for resource in resources if isinstance(resource, SiteRack)}
    declared = {resource.name for resource in devices}
    for name in store.names():
        if name in racks:
            _refuse_rack_setup(racks[name])
        if name not in declared:
            raise ValueError(f"devices/{name}.json names no device in the topology.")

    assigned: list[AssignedDevice] = []
    for device in devices:
        if device.KIND not in SIM_DRIVER_CLASS_BY_KIND:
            continue
        setup = _with_client(device.name, store.read(device.name), store.default_client)
        check_device_setup(device.name, device.KIND, setup)
        deck_modeling = isinstance(device, Device) and device.deck_modeling
        assigned.append(AssignedDevice(name=device.name, kind=device.KIND, deck_modeling=deck_modeling, setup=setup))
    return assigned


def check_setup_fits(
    resources: Iterable[Device | Transporter | SiteRack], name: str, setup: DeviceSetup, default_client: str,
) -> None:
    """Refuse a setup for `name` that the next package load would refuse."""
    resource = next((r for r in resources if r.name == name), None)
    if resource is None:
        raise ValueError(f"devices/{name}.json names no device in the topology.")
    if isinstance(resource, SiteRack):
        _refuse_rack_setup(resource)
    elif resource.KIND in SIM_DRIVER_CLASS_BY_KIND:
        check_device_setup(name, resource.KIND, _with_client(name, setup, default_client))


def _checked_setup(store: IDeviceSetupStore, kind: str, name: str) -> DeviceSetup:
    setup = store.read(name)
    check_device_setup(name, kind, setup)
    return setup


def interfaces_from_setups(store: IDeviceSetupStore) -> Callable[[str, str, bool], frozenset[str]]:
    """What a device's driver will implement, read from its setup before any orca-client has connected.

    An always_sim device only ever runs on its simulator, so it gets the simulator's interfaces.
    Any other device with a real driver gets the real driver's, whatever its sim or live setting.
    """

    def interfaces(kind: str, name: str, deck_modeling: bool) -> frozenset[str]:
        setup = _checked_setup(store, kind, name)
        if setup.always_sim:
            return sim_driver_interfaces(kind, setup, deck_modeling)
        return driver_interfaces(kind, setup, deck_modeling)

    return interfaces


def simulator_interfaces_from_setups(store: IDeviceSetupStore) -> Callable[[str, str, bool], frozenset[str]]:
    """What a device's simulator will implement, for a check run, which runs every device on its simulator."""

    def interfaces(kind: str, name: str, deck_modeling: bool) -> frozenset[str]:
        return sim_driver_interfaces(kind, _checked_setup(store, kind, name), deck_modeling)

    return interfaces
