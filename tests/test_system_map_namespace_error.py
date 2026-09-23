"""Pin the namespace-clarity error message in SystemMap.get_location.

The original Claude Desktop authoring session complained: ``Location X
does not exist in system map`` fires when the operator confuses a
``DeckResourceConfig.name`` (deck slot) or a ``PlateTemplate.name``
(template label) with a ``Topology.locations`` key (a site).

The original message didn't say which namespace was the issue or list
the alternatives, so the operator burned iterations re-editing
``topology.py`` when the bug was actually in a workflow's
``start=`` value.

This test pins the new error shape:
  * names the topology-site namespace (not just "location")
  * lists configured sites
  * names the two confusable namespaces and rejects them
"""

import pytest

from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.system.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap


async def _system_map_with_one_site() -> SystemMap:
    """A SystemMap with a single site so we can probe the unknown-site error."""
    registry = ResourceRegistry()
    sm = SystemMap(registry)
    await sm.initialize_transporters()
    pad = PlatePad("pad_1")
    await sm.add_location(Location("pad_1", resource=pad))
    return sm


async def test_unknown_location_error_names_topology_site_namespace() -> None:
    """The error must use the user-friendly 'site' wording and call out
    that the lookup is against Topology.locations, not deck slots or
    template names."""
    sm = await _system_map_with_one_site()
    with pytest.raises(KeyError) as excinfo:
        sm.get_location("dmso_reservoir")
    msg = str(excinfo.value)
    assert "Topology site" in msg
    assert "dmso_reservoir" in msg
    assert "not found in system map" in msg
    assert "Topology.locations" in msg


async def test_unknown_location_error_lists_configured_sites() -> None:
    """Operator should see the available alternatives without grepping."""
    sm = await _system_map_with_one_site()
    with pytest.raises(KeyError) as excinfo:
        sm.get_location("dmso_reservoir")
    assert "pad_1" in str(excinfo.value)


async def test_unknown_location_error_disambiguates_other_namespaces() -> None:
    """The error must name the two confusable namespaces so the
    operator knows where to look for their actual bug."""
    sm = await _system_map_with_one_site()
    with pytest.raises(KeyError) as excinfo:
        sm.get_location("dmso_reservoir")
    msg = str(excinfo.value)
    assert "DeckResourceConfig.name" in msg
    assert "PlateTemplate.name" in msg
    assert "DIFFERENT NAMESPACES" in msg


async def test_bad_deck_site_lists_the_devices_real_sites() -> None:
    """A '<device>/<bad-site>' miss must list the device's real sites so the
    author gets the right one. Under the flat model deck sites ARE graph nodes,
    so they appear in the configured list directly (no separate deck hint)."""
    sm = await _system_map_with_a_two_site_device()

    with pytest.raises(KeyError) as excinfo:
        sm.get_location("mlstar/carrier-9-9")
    msg = str(excinfo.value)
    assert "mlstar/carrier-7-0" in msg
    assert "mlstar/carrier-7-2" in msg


async def test_journey_start_on_a_device_mutex_is_rejected() -> None:
    """The flat-model home of the old ExecutingWorkflow resident-start guard:
    a thread start/end on a bare multi-site device is rejected at resolve time,
    so a resident can never be authored at a device endpoint that owns sites."""
    sm = await _system_map_with_a_two_site_device()

    with pytest.raises(KeyError, match="names a multi-site device"):
        sm.resolve_journey_location("mlstar")


class _Owner:
    def __init__(self, name: str) -> None:
        self.name = name


async def _system_map_with_a_two_site_device() -> SystemMap:
    """A device modelled as an off-graph mutex plus two flat site nodes, the
    shape ``build`` produces for a multi-site liquid handler."""
    from orca.resource_models.deck_site import DeckSite
    from orca.resource_models.deck_site_location import DeckSiteLocation

    registry = ResourceRegistry()
    sm = SystemMap(registry)
    await sm.initialize_transporters()
    sm.register_mutex_location(Location("mlstar", resource=PlatePad("mlstar")))
    for site in ("mlstar/carrier-7-0", "mlstar/carrier-7-2"):
        await sm.add_site_location(DeckSiteLocation(
            site, owner=_Owner("mlstar"), resource=DeckSite(site),
            mutex_position_id="mlstar",
        ))
    return sm


async def test_placement_on_a_device_mutex_is_rejected() -> None:
    """An operator placing on a bare multi-site device name must be refused,
    not silently landed on the off-graph mutex (which would then block every
    action on that device for the life of the process)."""
    sm = await _system_map_with_a_two_site_device()

    with pytest.raises(KeyError, match="names a multi-site device"):
        sm.resolve_placement_location("mlstar")


async def test_placement_resolves_a_specific_site() -> None:
    """The site-qualified form is the correct way to place, and it resolves to
    the real site node rather than raising."""
    sm = await _system_map_with_a_two_site_device()

    site = sm.resolve_placement_location("mlstar/carrier-7-0")
    assert site.position_id == "mlstar/carrier-7-0"


async def _system_map_with_a_single_site_device() -> SystemMap:
    """A device modelled as an off-graph mutex plus exactly ONE flat site node."""
    from orca.resource_models.deck_site import DeckSite
    from orca.resource_models.deck_site_location import DeckSiteLocation

    registry = ResourceRegistry()
    sm = SystemMap(registry)
    await sm.initialize_transporters()
    sm.register_mutex_location(Location("shaker_1", resource=PlatePad("shaker_1")))
    await sm.add_site_location(DeckSiteLocation(
        "shaker_1/nest", owner=_Owner("shaker_1"), resource=DeckSite("shaker_1/nest"),
        mutex_position_id="shaker_1",
    ))
    return sm


async def test_bare_name_of_single_site_device_resolves_to_its_site() -> None:
    """A single-site device addressed by its bare name resolves to its one site
    (symmetric with the single-alias path), instead of being misreported as a
    multi-site device and rejected."""
    sm = await _system_map_with_a_single_site_device()

    assert sm.resolve_journey_location("shaker_1").position_id == "shaker_1/nest"
    assert sm.resolve_placement_location("shaker_1").position_id == "shaker_1/nest"
