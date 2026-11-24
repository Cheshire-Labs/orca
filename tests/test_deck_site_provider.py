from collections.abc import Sequence

from cheshire_drivers.deck_site_units import DeckSiteProvider

from orca.system.deck_sites import _deck_site_rows


class _SingleSiteUnit:
    """A placement unit exposing exactly one deck-site."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def site_identifiers(self) -> Sequence[str]:
        return ("slot",)


class _TwoSiteUnit:
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def site_identifiers(self) -> Sequence[str]:
        return ("a", "b")


def test_single_site_provider_yields_exactly_one_row() -> None:
    """A one-site unit enumerates to a single (site_name, parent_name, 0) row."""
    unit: DeckSiteProvider = _SingleSiteUnit("C2")
    assert list(_deck_site_rows(unit)) == [("C2-slot", "C2", 0)]


def test_multi_site_provider_yields_one_row_per_site_with_a_per_unit_index() -> None:
    unit: DeckSiteProvider = _TwoSiteUnit("u1")
    assert list(_deck_site_rows(unit)) == [("u1-a", "u1", 0), ("u1-b", "u1", 1)]
