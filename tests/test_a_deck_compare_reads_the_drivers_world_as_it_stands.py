"""An operator's deck compare reads a driver world in doubt as it stands, so it can show where the driver left a plate.

A command that got no answer marks its world in doubt, and the remedy for it is to compare the deck before anything
rebuilds that world from the record. So the compare never rebuilds a world in doubt first.
"""

from unittest.mock import patch

import pytest
from cheshire_drivers.liquid_handler_models import DeckResourceConfig, ReconcileDeckOccupancyRequest

from orca.system.system_interface import DeckConflictReason
from tests.test_a_clear_survives_a_handler_that_is_down import (
    _DriverThatCanGoDown,
    _deck_names,
    _runtime_with_a_plate_on_the_sim_deck,
)


@pytest.mark.out_of_run
@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_deck_compare_in_the_world_in_force_agrees_after_a_discharge() -> None:
    live = _DriverThatCanGoDown()
    runtime, registered = await _runtime_with_a_plate_on_the_sim_deck(live, _DriverThatCanGoDown())
    try:
        assert registered.name in await _deck_names(live), "the live deck holds the plate before the discharge"

        await runtime.labware.discharge_labware(registered.id, force=True, confirm=True)
        comparison = await runtime.devices.compare_deck("lh")

        assert comparison.agrees and not comparison.disagreements, comparison
    finally:
        await runtime.shutdown()


@pytest.mark.out_of_run
@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_after_a_command_got_no_answer_compare_deck_shows_where_the_driver_left_the_plate() -> None:
    live = _DriverThatCanGoDown()
    runtime, registered = await _runtime_with_a_plate_on_the_sim_deck(live, _DriverThatCanGoDown())
    try:
        # The command moved the plate on the driver, then its answer never came: the pipeline marks the world.
        await live.reconcile_deck_occupancy(ReconcileDeckOccupancyRequest(resources=[DeckResourceConfig(
            name=registered.name, catalog_ref="Cor_Falcon_96_wellplate_340ul_Fb_Black",
            parent_id="carrier-7", site_index=1,
        )]))
        await runtime.system.device_worlds.mark_current_world("lh")

        with (
            patch.object(live, "configure_deck", wraps=live.configure_deck) as configured,
            patch.object(live, "reconcile_deck_occupancy", wraps=live.reconcile_deck_occupancy) as reconciled,
        ):
            comparison = await runtime.devices.compare_deck("lh")

        assert [(d.labware_name, d.reason, d.driver_site) for d in comparison.disagreements] == [
            (registered.name, DeckConflictReason.DRIVER_SITE_DIFFERS, "carrier-7-1"),
        ], "the compare must show where the driver left the plate, not the record it would rebuild from"
        assert configured.await_count == 0 and reconciled.await_count == 0, "the compare rebuilt the world it reads"
    finally:
        await runtime.shutdown()
