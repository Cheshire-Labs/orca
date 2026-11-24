"""Resolving the device behind a location."""

from typing import Optional

from orca.resource_models.device_deck_site import DeviceDeckSite
from orca.resource_models.devices import Device
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
