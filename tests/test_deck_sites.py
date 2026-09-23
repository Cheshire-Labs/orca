from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig
from cheshire_drivers.labware_seed import get_carrier_site_identifiers

from orca.system.deck_sites import enumerate_deck_sites


def test_enumerate_deck_sites_pins_tuple_shape_and_per_carrier_index() -> None:
    """build_system and reconcile_lh_deck_occupancy depend on this exact
    (site_name, carrier_name, site_index) contract; site_index is per-carrier
    and 0-based (reconcile feeds it straight into DeckResourceConfig.site_index).
    Pin it so a reorder or a global-index change can't silently desync them."""
    config = DeckLayoutConfig(
        deck_type="STARlet",
        resources=[
            DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
            DeckResourceConfig(name="carrier-25", catalog_ref="Trough_CAR_4R200_A00", rail=25),
        ],
    )

    rows = list(enumerate_deck_sites(config))

    expected = [
        (f"carrier-7-{site_id}", "carrier-7", index)
        for index, site_id in enumerate(get_carrier_site_identifiers("PLT_CAR_L5AC_A00"))
    ] + [
        (f"carrier-25-{site_id}", "carrier-25", index)
        for index, site_id in enumerate(get_carrier_site_identifiers("Trough_CAR_4R200_A00"))
    ]
    assert rows == expected
