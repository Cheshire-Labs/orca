from collections.abc import Iterator

from cheshire_drivers import DeckLayoutConfig
from cheshire_drivers.deck_site_units import DeckSiteProvider, deck_site_units


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
    for unit in deck_site_units(deck_config):
        yield from _deck_site_rows(unit)
