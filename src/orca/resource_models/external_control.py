"""Resolve the underlying Device behind a Location for external-control gating."""

from typing import Optional

from orca.resource_models.deck_access import device_behind
from orca.resource_models.devices import Device
from orca.resource_models.location import Location


def device_under_external_control(location: Location) -> Optional[Device]:
    """Return the underlying ``Device`` if it's under external control, else None."""
    device = device_behind(location)
    if device is not None and device.under_external_control:
        return device
    return None
