from collections.abc import Iterator, Sequence
from typing import Protocol

from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig
from cheshire_drivers.labware_seed import get_carrier_site_identifiers
from cheshire_drivers.plr_wrappers import slot_names_for_deck_type


class DeckSiteProvider(Protocol):
    """A placement unit on a liquid-handler deck that exposes its deck-sites.

    A Hamilton carrier exposes several sites; an Opentrons slot or fixture exposes exactly one.
    ``enumerate_deck_sites`` turns each provider's sites into the canonical deck-site tuples that
    the system build and deck-occupancy reconciliation share, so a new deck kind is added by
    supplying a provider rather than special-casing the enumeration.
    """

    @property
    def name(self) -> str: ...

    def site_identifiers(self) -> Sequence[str]: ...


class _HamiltonCarrierUnit:
    """A Hamilton carrier: a rail-mounted deck resource whose sites come from the seed catalog."""

    def __init__(self, config: DeckResourceConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return self._config.name

    def site_identifiers(self) -> Sequence[str]:
        return get_carrier_site_identifiers(self._config.catalog_ref)


class _OpentronsSlotUnit:
    """An Opentrons deck slot: a placement unit exposing exactly one deck-site."""

    def __init__(self, slot: str) -> None:
        self._slot = slot

    @property
    def name(self) -> str:
        return self._slot

    def site_identifiers(self) -> Sequence[str]:
        return ("slot",)


def _deck_site_units(deck_config: DeckLayoutConfig) -> Iterator[DeckSiteProvider]:
    """The deck-site placement units of a layout: Hamilton carriers declared on rails, or the
    built-in slots of a slot deck (Opentrons), which are deck structure rather than layout entries.
    """
    carriers = [r for r in deck_config.resources if r.rail is not None]
    if carriers:
        for carrier in carriers:
            yield _HamiltonCarrierUnit(carrier)
        return
    slot_names = slot_names_for_deck_type(deck_config.deck_type)
    if slot_names is not None:
        for slot in slot_names:
            yield _OpentronsSlotUnit(slot)


def _deck_site_rows(unit: DeckSiteProvider) -> Iterator[tuple[str, str, int]]:
    for index, site_id in enumerate(unit.site_identifiers()):
        yield f"{unit.name}-{site_id}", unit.name, index


def enumerate_deck_sites(
    deck_config: DeckLayoutConfig,
) -> Iterator[tuple[str, str, int]]:
    """Yield ``(site_name, parent_name, site_index)`` for every deck-site on the deck.

    ``site_name`` is the canonical ``{parent}-{site}`` deck-site label that ``build_system`` turns
    into a child Location and that deck-occupancy reconciliation matches against. Single-sourced
    here so those two callers cannot drift -- if they did, a resident reagent would silently stop
    matching its deck site and never project onto the driver deck.
    """
    for unit in _deck_site_units(deck_config):
        yield from _deck_site_rows(unit)
