"""Stored form of a device command whose outcome is unknown."""

from dataclasses import dataclass


@dataclass(frozen=True)
class StoredMotion:
    """Where a command states it moves a labware, kept by position id."""

    labware_id: str
    source_position_id: str | None
    target_position_id: str
    carried: str


@dataclass(frozen=True)
class StoredSiteNotice:
    """The site a command states it tells, kept by position id."""

    position_id: str
    labware_id: str | None
    mover_name: str
    event: str


@dataclass(frozen=True)
class UnheldCommand:
    """A command whose outcome is unknown and that nothing holds: what mark complete applies."""

    device_name: str
    motion: StoredMotion | None
    site: StoredSiteNotice | None
