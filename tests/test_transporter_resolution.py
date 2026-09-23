"""Tests for Transporter cloud-side teachpoint + gateway resolution.

The refactor moves teachpoint name -> coords resolution and gateway-path
walking out of the driver layer and into orca-core's Transporter.pick / .place.
The driver receives a fully-resolved PickAtCoordsRequest / PlaceAtCoordsRequest;
no name lookup happens on the driver side.

These tests assert: (1) Transporter.pick dispatches pick_at_coords with a
resolved teachpoint and gateway_path, (2) Transporter.place dispatches
place_at_coords analogously, (3) gateway chains are walked outermost-first,
(4) circular gateways raise, (5) missing gateway teachpoints raise, (6)
missing destination teachpoints raise.

The test driver subclasses `SimTransporterDriver` and overrides
`pick_at_coords` / `place_at_coords` to ONLY record dispatches (skipping
the sim's PLR-graph validation), so the tests can focus on the
Transporter's resolution behavior without seeding sim positions.
"""

from typing import List, Optional

import pytest

from cheshire_drivers import (
    AccessConfig,
    CartesianCoordinates,
    SimTransporterDriver,
    Teachpoint,
)
from orca.runtime.teachpoint_service import seeded_teachpoint_service
from cheshire_drivers.transporter_models import (
    PickAtCoordsRequest,
    PlaceAtCoordsRequest,
)

from orca.resource_models.labware import LabwareInstance, PlateTemplate
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.sim_labware import SimPlateTemplate
from tests.test_helpers import (
    make_transporter_with_driver,
)


_DEFAULT_VERTICAL = AccessConfig(name="default_vertical", access_type="vertical")


def _tp(name: str, gateway: Optional[str] = None) -> Teachpoint:
    return Teachpoint(
        name,
        CartesianCoordinates(x=100.0, y=0.0, z=50.0, yaw=180.0, pitch=90.0, roll=0.0),
        orientation="right",
        access=_DEFAULT_VERTICAL,
        gateway=gateway,
    )


class _RecordingDriver(SimTransporterDriver):
    """Records every pick_at_coords / place_at_coords dispatch.

    Overrides the sim's PLR-graph validation so resolution-focused tests
    don't have to seed positions. Other inherited methods (initialize,
    home, etc.) keep their sim behavior.
    """

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.pick_calls: List[PickAtCoordsRequest] = []
        self.place_calls: List[PlaceAtCoordsRequest] = []

    async def pick_at_coords(self, request: PickAtCoordsRequest) -> None:
        self.pick_calls.append(request)

    async def place_at_coords(self, request: PlaceAtCoordsRequest) -> None:
        self.place_calls.append(request)


class _RaisingDriver(SimTransporterDriver):
    """pick_at_coords always raises. Used to assert retry-state invariants."""

    async def pick_at_coords(self, request: PickAtCoordsRequest) -> None:
        raise RuntimeError("boom: simulated driver failure")

    async def place_at_coords(self, request: PlaceAtCoordsRequest) -> None:
        raise RuntimeError("boom: simulated driver failure")


class _RecordingThenRaisingDriver(SimTransporterDriver):
    """pick_at_coords succeeds (records); place_at_coords raises.

    Used to assert that a place failure leaves the gripper's held labware
    intact so retry paths can re-attempt place.
    """

    async def pick_at_coords(self, request: PickAtCoordsRequest) -> None:
        return None

    async def place_at_coords(self, request: PlaceAtCoordsRequest) -> None:
        raise RuntimeError("boom: simulated place failure")


async def _location_with_labware(name: str) -> Location:
    template = SimPlateTemplate("plate")
    instance = await template.create_instance()
    pad = PlatePad(name)
    location = Location(name, pad)
    # Resource-level initialize_labware stays sync; Location-level is now
    # async. This helper sets up the location's labware state without
    # firing observers (no transporter is bound here).
    pad.initialize_labware(instance)
    return location


def _empty_location(name: str) -> Location:
    pad = PlatePad(name)
    return Location(name, pad)


async def _load_gripper(transporter: Transporter, source: Location) -> LabwareInstance:
    """Stand in for the placement chokepoint, which is what records a plate in
    the jaws. The mover reads that record and never writes it."""
    labware = source.labware
    assert labware is not None
    await source.dispose_labware(labware)
    await transporter.gripper_location.place_labware(labware)
    return labware


class TestPickResolutionWithoutGateway:
    """Transporter.pick resolves the destination teachpoint and dispatches
    pick_at_coords with an empty gateway_path when the teachpoint has no
    gateway."""

    @pytest.mark.asyncio
    async def test_pick_dispatches_pick_at_coords(self) -> None:
        store = seeded_teachpoint_service()
        await store.add(_tp("pad_1"))
        driver = _RecordingDriver("arm")
        transporter = make_transporter_with_driver("arm", driver=driver, teachpoint_store=store)
        location = await _location_with_labware("pad_1")

        await transporter.pick(location)

        assert len(driver.pick_calls) == 1
        request = driver.pick_calls[0]
        assert isinstance(request, PickAtCoordsRequest)
        # Full Teachpoint payload preserved (not just the name).
        source_tp = await store.resolve("pad_1")
        assert source_tp is not None
        assert request.teachpoint.position_id == source_tp.position_id
        assert request.teachpoint.coordinates == source_tp.coordinates
        assert request.teachpoint.access_type == source_tp.access_type
        assert request.teachpoint.orientation == source_tp.orientation
        assert request.teachpoint.gripper_offset == source_tp.gripper_offset
        assert request.teachpoint.vertical_clearance == source_tp.vertical_clearance
        # labware_type comes from the source location's labware.
        assert location.labware is not None
        assert request.labware_type == location.labware.labware_type
        assert request.gateway_path == []

    @pytest.mark.asyncio
    async def test_pick_does_not_write_its_own_jaws(self) -> None:
        """The placement chokepoint records the plate in the jaws. A mover that
        wrote them too would be a second copy of the same fact."""
        store = seeded_teachpoint_service()
        await store.add(_tp("pad_1"))
        driver = _RecordingDriver("arm")
        transporter = make_transporter_with_driver("arm", driver=driver, teachpoint_store=store)
        location = await _location_with_labware("pad_1")

        await transporter.pick(location)

        assert len(driver.pick_calls) == 1
        assert transporter.labware is None

    @pytest.mark.asyncio
    async def test_pick_dispatch_raises_keeps_labware_none(self) -> None:
        """Retry-detection invariant: if the driver's pick dispatch raises,
        `Transporter._labware` MUST remain None so `ExecutableMoveAction`'s
        retry path re-picks rather than skipping pick on the next attempt.
        """
        store = seeded_teachpoint_service()
        await store.add(_tp("pad_1"))
        driver = _RaisingDriver("arm")
        transporter = make_transporter_with_driver("arm", driver=driver, teachpoint_store=store)
        location = await _location_with_labware("pad_1")

        with pytest.raises(RuntimeError, match="boom"):
            await transporter.pick(location)

        assert transporter.labware is None


class TestPlaceResolutionWithoutGateway:
    """Transporter.place resolves the destination teachpoint and dispatches
    place_at_coords with an empty gateway_path when the teachpoint has no
    gateway."""

    @pytest.mark.asyncio
    async def test_place_dispatches_place_at_coords(self) -> None:
        store = seeded_teachpoint_service()
        await store.add(_tp("pad_1"))
        await store.add(_tp("pad_2"))
        driver = _RecordingDriver("arm")
        transporter = make_transporter_with_driver("arm", driver=driver, teachpoint_store=store)
        source = await _location_with_labware("pad_1")
        await transporter.pick(source)
        held_labware_type = (await _load_gripper(transporter, source)).labware_type

        target = _empty_location("pad_2")
        await transporter.place(target)

        assert len(driver.place_calls) == 1
        request = driver.place_calls[0]
        assert isinstance(request, PlaceAtCoordsRequest)
        # Full Teachpoint payload preserved.
        source_tp = await store.resolve("pad_2")
        assert source_tp is not None
        assert request.teachpoint.position_id == source_tp.position_id
        assert request.teachpoint.coordinates == source_tp.coordinates
        assert request.teachpoint.access_type == source_tp.access_type
        # labware_type comes from the gripper's held labware (which the source
        # pad held before the pick).
        assert request.labware_type == held_labware_type
        assert request.gateway_path == []

    @pytest.mark.asyncio
    async def test_place_dispatch_raises_keeps_labware(self) -> None:
        """Symmetric retry-detection invariant for place: if the driver's
        place dispatch raises, `Transporter._labware` MUST remain set to
        the gripper's held labware so the retry path can re-attempt place
        without losing the labware reference.
        """
        store = seeded_teachpoint_service()
        await store.add(_tp("pad_1"))
        await store.add(_tp("pad_2"))
        driver = _RecordingThenRaisingDriver("arm")
        transporter = make_transporter_with_driver("arm", driver=driver, teachpoint_store=store)
        # Pick succeeds (driver pick_at_coords records, doesn't raise).
        source = await _location_with_labware("pad_1")
        await transporter.pick(source)
        held_before_place = await _load_gripper(transporter, source)

        # Place raises.
        target = _empty_location("pad_2")
        with pytest.raises(RuntimeError, match="boom"):
            await transporter.place(target)

        # Labware reference must survive the raise — retry depends on this.
        assert transporter.labware is held_before_place


class TestGatewayChainResolution:
    """Gateway chains are walked outermost-first."""

    @pytest.mark.asyncio
    async def test_single_gateway_returns_one_waypoint(self) -> None:
        store = seeded_teachpoint_service()
        await store.add(_tp("hotel_entry"))
        await store.add(_tp("nest_1", gateway="hotel_entry"))
        driver = _RecordingDriver("arm")
        transporter = make_transporter_with_driver("arm", driver=driver, teachpoint_store=store)
        location = await _location_with_labware("nest_1")

        await transporter.pick(location)

        request = driver.pick_calls[0]
        assert [wp.position_id for wp in request.gateway_path] == ["hotel_entry"]

    @pytest.mark.asyncio
    async def test_chained_gateways_returned_outermost_first(self) -> None:
        # nest_1.gateway = hotel_outer; hotel_outer.gateway = rail_entry.
        # Outermost-first means [rail_entry, hotel_outer].
        store = seeded_teachpoint_service()
        await store.add(_tp("rail_entry"))
        await store.add(_tp("hotel_outer", gateway="rail_entry"))
        await store.add(_tp("nest_1", gateway="hotel_outer"))
        driver = _RecordingDriver("arm")
        transporter = make_transporter_with_driver("arm", driver=driver, teachpoint_store=store)
        location = await _location_with_labware("nest_1")

        await transporter.pick(location)

        request = driver.pick_calls[0]
        assert [wp.position_id for wp in request.gateway_path] == ["rail_entry", "hotel_outer"]


class TestResolutionFailures:
    """Resolution errors raise with informative messages."""

    @pytest.mark.asyncio
    async def test_missing_destination_teachpoint_raises(self) -> None:
        store = seeded_teachpoint_service()
        driver = _RecordingDriver("arm")
        transporter = make_transporter_with_driver("arm", driver=driver, teachpoint_store=store)
        location = await _location_with_labware("ghost_pad")

        with pytest.raises(ValueError, match="no teachpoint registered for position_id 'ghost_pad'"):
            await transporter.pick(location)

    @pytest.mark.asyncio
    async def test_missing_gateway_teachpoint_raises(self) -> None:
        store = seeded_teachpoint_service()
        await store.add(_tp("nest_1", gateway="missing_gateway"))
        driver = _RecordingDriver("arm")
        transporter = make_transporter_with_driver("arm", driver=driver, teachpoint_store=store)
        location = await _location_with_labware("nest_1")

        with pytest.raises(
            ValueError,
            match="gateway teachpoint 'missing_gateway' is not registered",
        ):
            await transporter.pick(location)

    @pytest.mark.asyncio
    async def test_circular_gateway_raises(self) -> None:
        store = seeded_teachpoint_service()
        await store.add(_tp("a", gateway="b"))
        await store.add(_tp("b", gateway="a"))
        driver = _RecordingDriver("arm")
        transporter = make_transporter_with_driver("arm", driver=driver, teachpoint_store=store)
        location = await _location_with_labware("a")

        with pytest.raises(ValueError, match="circular gateway reference"):
            await transporter.pick(location)


class TestAnUnreachableTaughtPositionExplainsItself:
    """The bench cost two failed submissions to a bare `Target x cannot be
    reached from given sources`: the operator had just taught the position they
    were told was unreachable, and nothing connected the two."""

    async def test_the_refusal_names_the_stale_route_graph(self) -> None:
        from orca.system.resource_registry import ResourceRegistry
        from orca.system.system_map import (
            RouteGraphMissingTeachpointError, SystemMap,
        )
        from orca.resource_models.plate_pad import PlatePad

        system_map = SystemMap(ResourceRegistry())
        await system_map.add_location(Location("pad_a", PlatePad("pad_a")))
        await system_map.add_location(Location("pad_b", PlatePad("pad_b")))

        with pytest.raises(RouteGraphMissingTeachpointError, match="predates the teachpoint"):
            system_map.get_all_shortest_any_paths("pad_a", "pad_b")

    async def test_an_unknown_position_is_not_blamed_on_the_graph(self) -> None:
        """A typo is not a stale graph; saying so would send the operator to
        reload the runtime over a name that was never a position."""
        import networkx as nx
        from orca.system.resource_registry import ResourceRegistry
        from orca.system.system_map import (
            RouteGraphMissingTeachpointError, SystemMap,
        )
        from orca.resource_models.plate_pad import PlatePad

        system_map = SystemMap(ResourceRegistry())
        await system_map.add_location(Location("pad_a", PlatePad("pad_a")))

        with pytest.raises(nx.NetworkXNoPath) as caught:
            system_map.get_all_shortest_any_paths("pad_a", "typo_pad")
        assert not isinstance(caught.value, RouteGraphMissingTeachpointError)


class TestAPositionTaughtAfterStartupIsRoutable:
    """The bench taught a position, then every move to it failed. The graph is
    built from the teachpoints that existed at startup; a new one has to be
    wired in, not left in the store alone."""

    async def test_a_new_teachpoint_becomes_a_route(self) -> None:
        from orca.runtime.facades.teachpoints import TeachpointFacade
        from orca.system.resource_registry import ResourceRegistry
        from orca.system.system_map import SystemMap
        from orca.resource_models.plate_pad import PlatePad

        store = seeded_teachpoint_service()
        await store.add(_tp("pad_a"))
        await store.add(_tp("pad_b"))
        arm = make_transporter_with_driver(
            "arm", driver=_RecordingDriver("arm"), teachpoint_store=store,
        )
        registry = ResourceRegistry()
        registry.add_resource(arm)
        system_map = SystemMap(registry)
        for name in ("pad_a", "pad_b", "pad_c"):
            await system_map.add_location(Location(name, PlatePad(name)))
        await system_map.initialize_transporters()
        assert not system_map.has_any_route("pad_a", "pad_c")

        class _Host:
            def get_transporter(self, name: str) -> Transporter:
                return arm

            @property
            def transporters(self) -> list[Transporter]:
                return [arm]

            @property
            def system_map(self) -> SystemMap:
                return system_map

        await TeachpointFacade(_Host()).add("arm", _tp("pad_c"), confirm=True)

        assert system_map.has_any_route("pad_a", "pad_c")
        assert system_map.has_any_route("pad_c", "pad_b")
