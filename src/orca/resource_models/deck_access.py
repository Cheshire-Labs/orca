"""Resolving the device behind a location, and clearing its deck before an arm
reaches in."""

from typing import Optional, Protocol, runtime_checkable

from orca.resource_models.device_deck_site import DeviceDeckSite
from orca.resource_models.devices import Device
from orca.resource_models.labware_placeable_interface import IPlateMover
from orca.resource_models.labware_staging_bridge import LabwareStagingBridge
from orca.resource_models.location import Location


def device_behind(location: Location) -> Optional[Device]:
    """The ``Device`` this location belongs to, or None for a plain pad.

    Both device-backed site flavors must be resolved: ``LabwareStagingBridge``
    wraps every SDK-installed Device, and ``DeviceDeckSite`` backs each flat
    site of a deck-modeling handler. Neither is a ``Device``, so a naive
    ``isinstance(resource, Device)`` misses both production cases.
    """
    resource = location.resource
    if isinstance(resource, (LabwareStagingBridge, DeviceDeckSite)):
        return resource.device
    if isinstance(resource, Device):
        return resource
    return None


@runtime_checkable
class ClearsItsDeck(Protocol):
    """A device with something of its own moving over a deck an arm shares."""

    async def step_aside_for(self, mover: IPlateMover) -> None: ...


async def clear_the_deck_for(location: Location, mover: IPlateMover) -> None:
    """Make sure nothing of the device's own is over ``location`` before ``mover``
    reaches it. Blocks while the device is busy; a plain pad returns at once."""
    device = device_behind(location)
    if isinstance(device, ClearsItsDeck):
        await device.step_aside_for(mover)
