"""Tests for the teachpoint file store + service and the Transporter
store-ownership contract.

Post-Path-2 contract: the Transporter owns the teachpoint store and
resolves names against it on every `pick`/`place` dispatch via
`Transporter._resolve_teachpoint` / `_resolve_gateway_path`. Drivers
receive fully-resolved Teachpoint payloads (plus pre-walked gateway
paths) and never consult any store. `Transporter.get_teachpoints()`
reads from the store via async `list()` for the System graph builder.

Per-dispatch resolution behavior is exercised in
`tests/test_transporter_resolution.py`; this file covers the file store
and the constructor-time ownership wiring."""

from typing import List, Optional

import pytest

from tests.real_device_path import the_real_device_path
from cheshire_drivers import SimTransporterDriver
from cheshire_drivers.teachpoints import AccessConfig, CartesianCoordinates, Teachpoint
from cheshire_drivers.transporter_models import PickAtCoordsRequest, PlaceAtCoordsRequest

from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.resource_models.sim_labware import SimPlateTemplate
from orca.runtime.setup_file_stores import TeachpointFileStore
from orca.runtime.teachpoint_service import TeachpointService
from tests.test_helpers import make_transporter_with_driver
from tests.setup_package import fresh_teachpoints, teachpoint_file, with_teachpoints


class _RecordingDriver(SimTransporterDriver):
    """Records pick_at_coords dispatches, skipping the sim's PLR validation."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.pick_calls: List[PickAtCoordsRequest] = []
        self.place_calls: List[PlaceAtCoordsRequest] = []

    async def pick_at_coords(self, request: PickAtCoordsRequest) -> None:
        self.pick_calls.append(request)

    async def place_at_coords(self, request: PlaceAtCoordsRequest) -> None:
        self.place_calls.append(request)


async def _location_with_labware(name: str) -> Location:
    instance = await SimPlateTemplate("plate").create_instance()
    pad = PlatePad(name)
    location = Location(name, pad)
    pad.initialize_labware(instance)
    return location


_ACCESS_CONFIGS = [AccessConfig(
    name="default_vertical",
    access_type="vertical",
    gripper_offset=20.0,
    vertical_clearance=20.0,
    horizontal_clearance=100.0,
)]


def _tp(name: str, x: float = 0.0) -> Teachpoint:
    return Teachpoint(
        position_id=name,
        coordinates=CartesianCoordinates(x=x, y=0, z=0, yaw=0, pitch=90, roll=180),
        orientation="right",
        # A position reached by pick or place has to say which way the arm comes in;
        # these are open nests.
        access=_ACCESS_CONFIGS[0],
    )


def _store() -> TeachpointFileStore:
    return fresh_teachpoints(_ACCESS_CONFIGS)


class TestTeachpointFileStoreCrud:
    """Raw CRUD on the file store: no copy is kept, so every read goes back to the file."""

    async def test_add_rejects_inline_access_without_named_config(self) -> None:
        service = TeachpointService(_store())
        inline = Teachpoint(
            position_id="bad",
            coordinates=CartesianCoordinates(x=0, y=0, z=0, yaw=0, pitch=90, roll=180),
            orientation="right",
            access_type="vertical",
        )
        with pytest.raises(ValueError, match="named AccessConfig"):
            await service.add(inline)

    async def test_add_accepts_named_access_config(self) -> None:
        service = TeachpointService(_store())
        await service.add(_tp("ok"))
        loaded = await service.get("ok")
        assert loaded is not None
        assert loaded.access_config_name == _ACCESS_CONFIGS[0].name

    async def test_get_returns_none_for_unknown(self) -> None:
        assert await _store().get("missing") is None

    async def test_add_then_get_roundtrips(self) -> None:
        store = _store()
        await store.add(_tp("foo", x=42.0))
        loaded = await store.get("foo")
        assert loaded is not None
        assert loaded.position_id == "foo"
        assert isinstance(loaded.coordinates, CartesianCoordinates)
        assert loaded.coordinates.x == 42.0

    async def test_resolve_returns_flattened_teachpoint(self) -> None:
        store = _store()
        await store.add(_tp("a", x=7.0))
        resolved = await store.resolve("a")
        assert resolved is not None
        assert resolved.position_id == "a"
        assert isinstance(resolved.coordinates, CartesianCoordinates)
        assert resolved.coordinates.x == 7.0

    async def test_resolve_unknown_returns_none(self) -> None:
        assert await _store().resolve("missing") is None

    async def test_list_orders_by_position_id(self) -> None:
        store = _store()
        await store.add(_tp("b"))
        await store.add(_tp("a"))
        assert [tp.position_id for tp in await store.list()] == ["a", "b"]

    async def test_add_duplicate_name_raises(self) -> None:
        store = _store()
        await store.add(_tp("dup"))
        with pytest.raises(ValueError, match="already registered"):
            await store.add(_tp("dup"))

    async def test_update_replaces_value(self) -> None:
        store = _store()
        await store.add(_tp("foo", x=0.0))
        await store.update(_tp("foo", x=99.0))
        loaded = await store.get("foo")
        assert loaded is not None
        assert isinstance(loaded.coordinates, CartesianCoordinates)
        assert loaded.coordinates.x == 99.0

    async def test_update_unknown_raises(self) -> None:
        with pytest.raises(KeyError, match="not found"):
            await _store().update(_tp("ghost"))

    async def test_delete_returns_true_when_present(self) -> None:
        store = _store()
        await store.add(_tp("foo"))
        assert await store.delete("foo") is True
        assert await store.get("foo") is None

    async def test_delete_returns_false_when_absent(self) -> None:
        assert await _store().delete("ghost") is False


class TestTransporterStoreWiring:
    """Path 2 runtime contract: the Transporter owns the teachpoint store
    and resolves names against it on every `pick`/`place` dispatch. Drivers
    receive fully-resolved Teachpoint payloads (plus pre-walked gateway
    paths) and never consult any store on the dispatch path.
    `Transporter.get_teachpoints()` reads from the store via async `list()`
    for the System graph builder.

    Previous contract (pre-Path-2) bound the store onto each driver and
    drivers resolved names themselves. The driver-side `bind_teachpoint_store`
    hook is gone; the per-dispatch resolution path inside
    `Transporter.pick`/`.place` is exercised in
    `tests/test_transporter_resolution.py`.
    """

    async def test_get_teachpoints_returns_store_contents(self) -> None:
        """`Transporter.get_teachpoints()` is the System graph builder's view;
        it reads from the store, not the driver."""
        store = teachpoint_file([_tp("home"), _tp("waste")])
        transporter = with_teachpoints(Transporter("arm"), store)
        names = {tp.position_id for tp in await transporter.get_teachpoints()}
        assert names == {"home", "waste"}

    @pytest.mark.asyncio
    async def test_dispatch_sees_store_mutations_without_reprime(self) -> None:
        """A teachpoint added to the store AFTER construction must resolve on
        the next dispatch, with no reprime step.

        `Transporter.pick` resolves the destination name against the live
        store on every dispatch, so picking at a location added post-build
        succeeds and the resolved coordinates reach the driver. Picking at a
        name that is still unknown raises, proving the resolution is real
        (not a cached snapshot from construction time)."""
        store = teachpoint_file([_tp("home")])
        driver = _RecordingDriver("arm")
        transporter = with_teachpoints(make_transporter_with_driver(
            "arm", driver=driver, 
        ), store)

        # The freshly-added teachpoint did not exist when the transporter was
        # built; a dispatch must still resolve it.
        await store.add(_tp("waste", x=77.0))
        await the_real_device_path().connect_built()
        await transporter.pick(await _location_with_labware("waste"))

        assert len(driver.pick_calls) == 1
        request = driver.pick_calls[0]
        assert request.teachpoint.position_id == "waste"
        assert isinstance(request.teachpoint.coordinates, CartesianCoordinates)
        assert request.teachpoint.coordinates.x == 77.0

        # A still-unknown name fails resolution at dispatch, proving the lookup
        # is live. Fresh transporter sidesteps the gripper-occupied guard above.
        fresh = with_teachpoints(make_transporter_with_driver(
            "arm2", driver=_RecordingDriver("arm2"), 
        ), store)
        await the_real_device_path().connect_built()
        with pytest.raises(ValueError, match="No teachpoint registered"):
            await fresh.pick(await _location_with_labware("never_added"))

    async def test_a_transporter_outside_a_build_has_no_teachpoints_attached(self) -> None:
        transporter = Transporter("arm")
        with pytest.raises(RuntimeError, match="teachpoints/<name>.json"):
            await transporter.get_teachpoints()

