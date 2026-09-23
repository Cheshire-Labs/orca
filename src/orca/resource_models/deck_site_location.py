from typing import Optional, Protocol

from orca.resource_models.deck_site import DeckSite
from orca.resource_models.labware_placeable_interface import ILabwarePlaceable
from orca.resource_models.location import Location


class ISiteOwner(Protocol):
    """The device a site belongs to; the reservation ownership check and the
    present/stow wire both consult this one reference."""

    @property
    def name(self) -> str: ...


class DeckSiteLocation(Location):
    """Flat routing-graph site node owned by a device.

    Defaults its resource to a DeckSite so the node is never a
    deadlock-resolution park target.
    """

    def __init__(
        self,
        position_id: str,
        owner: ISiteOwner,
        resource: Optional[ILabwarePlaceable] = None,
        mutex_position_id: Optional[str] = None,
    ) -> None:
        super().__init__(position_id, resource if resource is not None else DeckSite(position_id))
        self._owner = owner
        self._mutex_position_id = mutex_position_id if mutex_position_id is not None else owner.name

    @property
    def owner(self) -> ISiteOwner:
        return self._owner

    @property
    def owner_mutex_id(self) -> Optional[str]:
        """The owning device's reservation mutex key."""
        return self._mutex_position_id
