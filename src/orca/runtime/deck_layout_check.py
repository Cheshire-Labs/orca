"""A deck layout declares carriers only: every resource in it must be a carrier the labware catalog knows."""

from cheshire_drivers.liquid_handler_models import DeckLayoutConfig

from orca.runtime.facades.labware_catalog import ILabwareCatalogFacade


async def carrier_labware_types(catalog: ILabwareCatalogFacade) -> set[str]:
    """Every labware_type the catalog files as a carrier, built in or added."""
    return {entry.labware_type for entry in await catalog.list("carrier")}


def deck_layout_errors(layout: DeckLayoutConfig, carriers: set[str]) -> list[str]:
    """One message per resource that is not a carrier. Labware reaches a deck through the ledger, never the layout."""
    return [
        f"Labware '{resource.name}' ({resource.catalog_ref}) must not be declared in the deck "
        f"layout. The deck config declares carriers only; route labware as a thread with "
        f"@orca.action(deck_positions=...) for transient labware, or a REUSE_EXISTING resident "
        f"for persistent reagents. Available carriers: {', '.join(sorted(carriers))}"
        for resource in layout.resources
        if resource.catalog_ref not in carriers
    ]
