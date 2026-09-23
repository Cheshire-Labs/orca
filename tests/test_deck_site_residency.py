"""Deck sites are exclusive place-until-pick resources.

Two transit plates routed to the SAME site on a multi-site deck must serialize:
the second cannot become resident until the first is picked off. Before the
residency reservation existed, exclusion was keyed at the device only, so a
plate could be placed onto a carrier still holding another (the multi-plate SMC
``Resource 'plate_1' already assigned to deck`` collision).

These guard the reservation layer directly, with no full workflow: the
invariant (``can_reserve`` over a site), the resident self-exemption, and the
place-until-pick window under the flat model.

Note: the exclusion is currently enforced by the reservation gate alone. The
hold-until-pick acquire that would keep a site reserved for the whole time a
plate sits on it does not exist.
"""
from tests.mock import EXTERNAL_MOVER

from unittest.mock import Mock

import asyncio
import pytest

from orca.resource_models.deck_site import DeckSite
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.reservation_manager import LocationReservationManager


SITE_ID = "mlstar_1/carrier-7-1"


def _site() -> Location:
    return Location(SITE_ID, DeckSite(SITE_ID))


def _registry(site: Location) -> Mock:
    reg = Mock()
    reg.get_location.return_value = site
    return reg


@pytest.mark.asyncio
async def test_occupied_deck_site_rejects_another_plate() -> None:
    """A carrier holding plate A rejects a reservation for plate B, and grants
    it only once A is picked off."""
    site = _site()
    manager = LocationReservationManager(_registry(site))
    plate_a = LabwareInstance("plate_1", "Cor_Falcon_96_wellplate_340ul_Fb_Black")
    plate_b = LabwareInstance("plate_1", "Cor_Falcon_96_wellplate_340ul_Fb_Black")

    site.initialize_labware(plate_a)

    res_b = LocationReservation(site, plate_b)
    await manager.attempt_reservation(SITE_ID, res_b, thread_id="thread-b")
    assert res_b.rejected.is_set() and not res_b.granted.is_set(), (
        "a second plate must not reserve a carrier still holding another plate"
    )

    await site.notify_picked(plate_a, EXTERNAL_MOVER)

    res_b2 = LocationReservation(site, plate_b)
    await manager.attempt_reservation(SITE_ID, res_b2, thread_id="thread-b")
    assert res_b2.granted.is_set(), (
        "the carrier must become reservable once the first plate is picked off"
    )


@pytest.mark.asyncio
async def test_resident_reserves_its_own_occupied_site() -> None:
    """A plate may reserve a carrier that already holds its OWN instance -- a
    deck resident (or a same-thread consecutive action) never blocks itself."""
    site = _site()
    manager = LocationReservationManager(_registry(site))
    reservoir = LabwareInstance("bead_reservoir", "AGenBio_1_troughplate_190000uL_Fl")

    site.initialize_labware(reservoir)

    res = LocationReservation(site, reservoir)
    await manager.attempt_reservation(SITE_ID, res, thread_id="thread-resident")
    assert res.granted.is_set(), (
        "own-labware occupancy must re-grant (can_reserve outcome 2), else a "
        "resident would deadlock waiting for its own site"
    )


async def test_occupied_deck_site_blocks_foreign_labware_until_departure() -> None:
    """The residency guarantee under the flat model:
    a plate occupying a deck site excludes other labware from reserving that
    site for exactly as long as it sits there; departure (the pick) frees it."""
    from orca.resource_models.deck_site_location import DeckSiteLocation
    from orca.system.reservation_manager.reservation_manager import (
        LocationReservationManager,
    )
    from orca.system.resource_registry import ResourceRegistry
    from orca.system.system_map import SystemMap

    class _Owner:
        name = "mlstar_1"

    system_map = SystemMap(ResourceRegistry())
    site = DeckSiteLocation(
        "mlstar_1/carrier-7-1", _Owner(), mutex_position_id="mlstar_1",
    )
    await system_map.add_site_location(site)
    manager = LocationReservationManager(system_map)

    resident = LabwareInstance("trough", "Nunc_96_wellplate_1300ul_Rb")
    site.initialize_labware(resident)

    assert manager.can_reserve(
        "mlstar_1/carrier-7-1", thread_id="t2", requesting_labware_id="other-lw"
    ) is False, "an occupied site must not be reservable for foreign labware"

    await site.notify_picked(resident, EXTERNAL_MOVER)
    assert manager.can_reserve(
        "mlstar_1/carrier-7-1", thread_id="t2", requesting_labware_id="other-lw"
    ) is True, "departure must free the site"
