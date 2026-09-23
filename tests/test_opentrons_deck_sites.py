from cheshire_drivers import DeckLayoutConfig

from orca.system.deck_sites import enumerate_deck_sites


def test_flex_layout_enumerates_the_decks_placeable_slots() -> None:
    """An Opentrons Flex layout declares no resources; its deck-sites are the deck's built-in slots
    minus the trash slot (A3). Each is a single-site unit whose occupancy projects as
    (parent_id=slot, site_index=0), reusing the carrier-site wire shape -- no new DTO field.

    The column-4 staging pads are placement sites like any other: a plate parked there
    is deck occupancy the engine has to model, even though the pipettes cannot reach it."""
    config = DeckLayoutConfig(deck_type="FlexDeck", resources=[])

    rows = list(enumerate_deck_sites(config))

    assert len(rows) == 15
    parents = {parent for _, parent, _ in rows}
    assert {"A1", "C2", "D3"} <= parents
    assert {"A4", "B4", "C4", "D4"} <= parents  # staging pads hold labware too
    assert "A3" not in parents  # trash slot is not a placement site
    assert all(site_index == 0 for *_, site_index in rows)
    assert all(site_name == f"{parent}-slot" for site_name, parent, _ in rows)


def test_ot2_layout_enumerates_the_decks_placeable_slots() -> None:
    """The OT-2 deck uses the same slot model: 1-based slots surface as "1".."11" (slot 12 is the
    trash), each projecting as (parent_id=slot, site_index=0). One enumeration path, both decks."""
    config = DeckLayoutConfig(deck_type="OTDeck", resources=[])

    rows = list(enumerate_deck_sites(config))

    assert len(rows) == 11
    parents = {parent for _, parent, _ in rows}
    assert {"1", "7", "11"} <= parents
    assert "12" not in parents  # trash slot is not a placement site
    assert all(site_index == 0 for *_, site_index in rows)
    assert all(site_name == f"{parent}-slot" for site_name, parent, _ in rows)
