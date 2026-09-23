from collections.abc import Sequence

from cheshire_drivers import DeckResourceConfig
from cheshire_drivers.labware_seed import get_carrier_site_identifiers

from orca.system.deck_sites import (
    DeckSiteProvider,
    _deck_site_rows,
    _HamiltonCarrierUnit,
)


class _SingleSiteUnit:
    """The Opentrons shape: a placement unit exposing exactly one deck-site."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def site_identifiers(self) -> Sequence[str]:
        return ("slot",)


def test_single_site_provider_yields_exactly_one_row() -> None:
    """The point of the provider seam: a one-site unit (an Opentrons slot/fixture) enumerates to a
    single (site_name, parent_name, 0) row, mirroring the multi-site Hamilton carrier shape."""
    unit: DeckSiteProvider = _SingleSiteUnit("C2")
    assert list(_deck_site_rows(unit)) == [("C2-slot", "C2", 0)]


def test_hamilton_carrier_unit_exposes_its_catalog_sites() -> None:
    """The Hamilton provider sources its sites from the seed catalog by catalog_ref, unchanged from
    the pre-seam inline walk."""
    config = DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7)
    unit = _HamiltonCarrierUnit(config)
    assert unit.name == "carrier-7"
    assert list(unit.site_identifiers()) == list(get_carrier_site_identifiers("PLT_CAR_L5AC_A00"))
    assert list(_deck_site_rows(unit)) == [
        (f"carrier-7-{site_id}", "carrier-7", index)
        for index, site_id in enumerate(get_carrier_site_identifiers("PLT_CAR_L5AC_A00"))
    ]
