"""Auto-derivation of LiquidHandler flat deck-site nodes from the deck config's carriers.

build_system walks the carriers (entries with rail set) in the LH's resolved
DeckLayoutConfig and asks the PLR catalog (via cheshire_drivers'
get_carrier_site_identifiers) for the site list. One flat DeckSiteLocation
"<lh>/<carrier>-<site>" is created per (carrier_name, site_identifier) pair.

Every derived site is equal: there is no designated handoff and no flag that
excludes a site from being a working target. Which sites the external arm can
reach is topology -- the arm teaches SITE-QUALIFIED points -- and the internal
gripper is a transporter meshing all deck sites pairwise, not a site node.

Labware-on-carrier-site entries (parent_id + site_index) in the deck config
are REJECTED at DeckLayoutConfig construction -- the deck config declares
carriers only, and labware belongs in the workflow (a thread with
deck_positions, or a REUSE_EXISTING resident). See the DeckResourceConfig
docstring and DeckLayoutConfig's validator.
"""

import pytest
from pydantic import ValidationError
from cheshire_drivers import (
    CartesianCoordinates,
    DeckLayoutConfig,
    DeckResourceConfig,
    Teachpoint,
)
from cheshire_drivers.labware_seed import get_carrier_site_identifiers

from orca.devices.devices import LiquidHandler, LiquidHandlerProtocol
from orca.resource_models.device_deck_site import DeviceDeckSite
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.registries.null_gateway_registry import NullDeviceConnectionSource
from orca.sdk.build import SystemBuild, Topology, build_system


_TIPRACK = "hamilton_96_tiprack_10uL_filter"
_PLATE = "Cor_Falcon_96_wellplate_340ul_Fb_Black"


async def _build_with_lh(
    deck_layout: DeckLayoutConfig | None,
    arm_teaches: str = "lh",
) -> SystemBuild:
    """Build a one-LH topology. ``arm_teaches`` is the point the external arm is
    taught: a deck LH is multi-site, so its arm point must be site-qualified."""
    stores = InMemoryRuntimeStoreFactory()
    if deck_layout is None:
        lh = LiquidHandler("lh")
    else:
        lh = LiquidHandler(
            "lh",
            deck_layout_store=stores.deck_layouts("lh", seed={"default": deck_layout}),
            deck_layout="default",
        )
    pad = PlatePad("pad")
    c = CartesianCoordinates
    arm = Transporter(
        "arm",
        teachpoint_store=stores.teachpoints("arm", seed=[
            Teachpoint(arm_teaches, c(0, 0, 0, 0, 90, 180), orientation="right"),
            Teachpoint("pad", c(100, 0, 0, 0, 90, 180), orientation="right"),
        ]),
    )
    topology = Topology(
        locations={"lh": lh, "pad": pad},
        transporters=[arm],
    )
    return await build_system(name="t", topology=topology, stores=stores)


def _site_names(system_build: SystemBuild) -> set[str]:
    return {site.name for site in system_build.system.system_map.sites_of("lh")}


def _expected_sites(carrier_name: str, catalog_ref: str) -> set[str]:
    return {
        f"lh/{carrier_name}-{site_id}"
        for site_id in get_carrier_site_identifiers(catalog_ref)
    }


class TestDeriveChildLocationsFromCarriers:

    async def test_single_carrier_yields_every_site(self) -> None:
        layout = DeckLayoutConfig(
            deck_type="STARlet",
            resources=[
                DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
            ],
        )
        build = await _build_with_lh(layout, arm_teaches="lh/carrier-7-0")

        assert _site_names(build) == _expected_sites("carrier-7", "PLT_CAR_L5AC_A00")

    async def test_multi_carrier_yields_union_of_sites(self) -> None:
        layout = DeckLayoutConfig(
            deck_type="STARlet",
            resources=[
                DeckResourceConfig(name="carrier-7",  catalog_ref="PLT_CAR_L5AC_A00",     rail=7),
                DeckResourceConfig(name="carrier-15", catalog_ref="TIP_CAR_480_A00",      rail=15),
                DeckResourceConfig(name="carrier-25", catalog_ref="Trough_CAR_4R200_A00", rail=25),
            ],
        )
        build = await _build_with_lh(layout, arm_teaches="lh/carrier-7-0")

        expected = (
            _expected_sites("carrier-7", "PLT_CAR_L5AC_A00")
            | _expected_sites("carrier-15", "TIP_CAR_480_A00")
            | _expected_sites("carrier-25", "Trough_CAR_4R200_A00")
        )
        assert _site_names(build) == expected

    async def test_the_arm_taught_site_is_an_ordinary_working_site(self) -> None:
        """The site the arm is taught is not special: it is a plain deck site,
        gripper-meshed to its siblings like every other, so it is a valid
        working / deck_positions target rather than a transient drop point.
        """
        layout = DeckLayoutConfig(
            deck_type="STARlet",
            resources=[
                DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
            ],
        )
        build = await _build_with_lh(layout, arm_teaches="lh/carrier-7-2")

        system_map = build.system.system_map
        taught = next(
            site for site in system_map.sites_of("lh") if site.name == "lh/carrier-7-2"
        )
        assert isinstance(taught.resource, DeviceDeckSite)
        # Same node kind and same gripper reachability as any sibling site.
        assert system_map.get_transporter_between(
            "lh/carrier-7-2", "lh/carrier-7-1"
        ).name == "lh/gripper"
        assert system_map.get_transporter_between(
            "lh/carrier-7-1", "lh/carrier-7-2"
        ).name == "lh/gripper"

    async def test_arm_teachpoint_naming_an_unknown_site_raises(self) -> None:
        """An arm point that names no derived deck site fails LOUD at wiring
        rather than auto-creating a node the plate could never actually reach."""
        layout = DeckLayoutConfig(
            deck_type="STARlet",
            resources=[
                DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
            ],
        )
        with pytest.raises(ValueError, match="neither a registered location"):
            await _build_with_lh(layout, arm_teaches="lh/carrier-7-99")

    async def test_arm_teachpoint_naming_the_bare_multi_site_device_raises(self) -> None:
        """A deck LH has many physical arm coordinates, so the bare device name
        cannot stand for one. Build demands the site-qualified point."""
        layout = DeckLayoutConfig(
            deck_type="STARlet",
            resources=[
                DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
            ],
        )
        with pytest.raises(ValueError, match="multi-site device"):
            await _build_with_lh(layout, arm_teaches="lh")

    async def test_no_layout_yields_only_the_single_slot(self) -> None:
        build = await _build_with_lh(None)
        assert _site_names(build) == {"lh/slot"}

    async def test_protocol_handler_derives_no_deck_sites(self) -> None:
        """A LiquidHandlerProtocol is deckless: build derives no deck sites and
        no on-deck gripper for it, even though it shares KIND with the deck
        LiquidHandler -- it gets only the generic single-slot bridge site every
        non-deck device gets. This is the sibling invariant the deck/protocol
        split rests on."""
        stores = InMemoryRuntimeStoreFactory()
        lh = LiquidHandlerProtocol("lh")
        pad = PlatePad("pad")
        c = CartesianCoordinates
        arm = Transporter(
            "arm",
            teachpoint_store=stores.teachpoints("arm", seed=[
                Teachpoint("lh",  c(0, 0, 0, 0, 90, 180), orientation="right"),
                Teachpoint("pad", c(100, 0, 0, 0, 90, 180), orientation="right"),
            ]),
        )
        topology = Topology(locations={"lh": lh, "pad": pad}, transporters=[arm])
        build = await build_system(name="t", topology=topology, stores=stores)
        assert _site_names(build) == {"lh/slot"}

    def test_labware_entries_in_deck_config_are_rejected(self) -> None:
        """A deck config carrying labware-on-carrier-site is rejected at construction.

        The deck config declares carriers only. Labware occupancy reaches the
        deck from the ledger (a thread with deck_positions, or a REUSE_EXISTING
        resident), never from the layout. cheshire-drivers' DeckLayoutConfig
        validator enforces this, so a config with parent_id/site_index entries
        raises a Pydantic ValidationError before build ever sees it.
        """
        with pytest.raises(ValidationError, match="declares labware on a carrier site"):
            DeckLayoutConfig(
                deck_type="STARlet",
                resources=[
                    DeckResourceConfig(name="carrier-7",  catalog_ref="PLT_CAR_L5AC_A00", rail=7),
                    DeckResourceConfig(name="carrier-15", catalog_ref="TIP_CAR_480_A00",  rail=15),
                    DeckResourceConfig(name="plate_a", catalog_ref=_PLATE,   parent_id="carrier-7",  site_index=0),
                    DeckResourceConfig(name="tips_a",  catalog_ref=_TIPRACK, parent_id="carrier-15", site_index=0),
                ],
            )

    async def test_unknown_carrier_catalog_ref_raises(self) -> None:
        layout = DeckLayoutConfig(
            deck_type="STARlet",
            resources=[
                DeckResourceConfig(name="carrier-x", catalog_ref="DOES_NOT_EXIST", rail=1),
            ],
        )
        with pytest.raises(KeyError, match="Unknown carrier catalog_ref"):
            await _build_with_lh(layout)

    async def test_site_resource_types_and_internal_gripper(self) -> None:
        layout = DeckLayoutConfig(
            deck_type="STARlet",
            resources=[
                DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
            ],
        )
        build = await _build_with_lh(layout, arm_teaches="lh/carrier-7-0")
        for site in build.system.system_map.sites_of("lh"):
            assert isinstance(site.resource, DeviceDeckSite)
        # The on-deck gripper is a transporter edge between deck sites,
        # not a site node.
        gripper = build.system.system_map.get_transporter_between(
            "lh/carrier-7-0", "lh/carrier-7-1"
        )
        assert gripper.name != "arm"


class TestFacadeListsDeckSites:
    """The location-listing snapshot surfaces a device's deck sites, so REST /
    CLI / MCP reveal them without a separate deck-layout call. Deck sites are
    flat routing nodes, so they appear in the listing as first-class locations;
    the bare device name is the off-graph reservation mutex and is not listed."""

    async def test_registry_facade_lists_deck_sites_for_the_liquid_handler(self) -> None:
        from orca.runtime.facades.registry import RegistryFacade

        layout = DeckLayoutConfig(
            deck_type="STARlet",
            resources=[
                DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
            ],
        )
        build = await _build_with_lh(layout, arm_teaches="lh/carrier-7-2")
        facade = RegistryFacade(
            system=build.system,
            list_reservations_fn=lambda eid: [],
            cancel_reservation_fn=lambda eid, rid: None,
            connections=NullDeviceConnectionSource(),
        )
        locs = {loc.name: loc for loc in facade.list_locations()}

        assert _expected_sites("carrier-7", "PLT_CAR_L5AC_A00") <= set(locs)
        assert "lh/gripper" not in locs
        assert "lh" not in locs
        assert "pad" in locs


class TestEmptySitesAreInert:
    """Carrier sites that no labware ever occupies still get flat site nodes.

    For trough-carrier sites and any other site the workflow never targets
    via deck_positions / REUSE_EXISTING, the site node exists but stays
    empty. These tests prove that's harmless: labware lookups return None,
    loaded_labware is empty, no routing reaches them.
    """

    async def test_trough_carrier_sites_are_empty_site_nodes(self) -> None:
        layout = DeckLayoutConfig(
            deck_type="STARlet",
            resources=[
                DeckResourceConfig(name="carrier-25", catalog_ref="Trough_CAR_4R200_A00", rail=25),
            ],
        )
        build = await _build_with_lh(layout, arm_teaches="lh/carrier-25-0")
        trough_sites = [
            site for site in build.system.system_map.sites_of("lh")
            if site.name.startswith("lh/carrier-25-")
        ]
        assert len(trough_sites) == 4
        for site in trough_sites:
            assert site.resource.labware is None
            assert site.resource.loaded_labware == []
