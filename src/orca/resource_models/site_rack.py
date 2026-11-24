from abc import abstractmethod

from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.resources import IResource


class SiteRack(IResource):
    """A passive resource that holds labware on sites of its own: each site is a location, the rack is not."""

    @property
    @abstractmethod
    def sites(self) -> list[PlatePad]: ...

    @abstractmethod
    def site_name(self, site: PlatePad) -> str:
        """The site's name on the rack (`shelf_3`), apart from its location name."""
