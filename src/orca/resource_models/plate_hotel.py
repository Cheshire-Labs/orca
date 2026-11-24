from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.site_rack import SiteRack


def shelf_site(shelf: int) -> str:
    """A shelf's site name on the hotel's model: `shelf_<n>`, counted from 1 at the bottom."""
    return f"shelf_{shelf}"


def shelf_location(hotel: str, shelf: int) -> str:
    """A shelf's location name: `<hotel>/shelf_<n>`."""
    return f"{hotel}/{shelf_site(shelf)}"


class PlateHotel(SiteRack):
    """A passive rack of shelves; each shelf is a plate pad at `<name>/shelf_<n>`, counted from 1 at the bottom."""

    def __init__(self, name: str, shelves: int) -> None:
        if shelves < 1:
            raise ValueError(f"a plate hotel needs at least one shelf, not {shelves}")
        self._name = name
        self._shelves = [PlatePad(shelf_location(name, shelf)) for shelf in range(1, shelves + 1)]
        self._site_names = {pad.name: shelf_site(shelf) for shelf, pad in enumerate(self._shelves, start=1)}

    @property
    def name(self) -> str:
        return self._name

    @property
    def shelves(self) -> list[PlatePad]:
        return list(self._shelves)

    @property
    def sites(self) -> list[PlatePad]:
        return self.shelves

    def site_name(self, site: PlatePad) -> str:
        return self._site_names[site.name]
