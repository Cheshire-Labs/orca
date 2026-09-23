"""A position remembers what it was taught with, and its per-labware exceptions.

Both fields are useless if they do not survive storage: the resolver reads them
off a teachpoint that came back out of the database on every single move.
"""

import pytest
from cheshire_drivers.move_parameters import MoveParameterPatch
from cheshire_drivers.teachpoints import AccessConfig, CartesianCoordinates, Teachpoint

from orca.runtime.db import create_memory_engine
from orca.runtime.sqlite_teachpoint_store import SqliteTeachpointStore
from orca.runtime.teachpoint_wire import build_teachpoint


pytestmark = pytest.mark.asyncio


def _tp(
    taught_with: str | None = None,
    by_labware: dict[str, MoveParameterPatch] | None = None,
) -> Teachpoint:
    return Teachpoint(
        position_id="hotel_3",
        coordinates=CartesianCoordinates(x=42.0, y=0, z=0, yaw=0, pitch=90, roll=180),
        orientation="right",
        access=AccessConfig(
            name="default_vertical",
            access_type="vertical",
            gripper_offset=20.0,
            vertical_clearance=20.0,
            horizontal_clearance=100.0,
        ),
        taught_with=taught_with,
        by_labware=by_labware,
    )


def _store() -> SqliteTeachpointStore:
    return SqliteTeachpointStore(create_memory_engine())


class TestTheyRoundTripThroughTheStore:
    async def test_taught_with_comes_back(self) -> None:
        store = _store()
        await store.add(_tp(taught_with="costar_96"))

        loaded = await store.get("hotel_3")

        assert loaded is not None
        assert loaded.taught_with == "costar_96"

    async def test_the_per_labware_overrides_come_back_as_patches(self) -> None:
        store = _store()
        await store.add(_tp(by_labware={"deep_well": MoveParameterPatch(clearance=45.0)}))

        loaded = await store.get("hotel_3")

        assert loaded is not None
        assert loaded.by_labware == {"deep_well": MoveParameterPatch(clearance=45.0)}

    async def test_a_position_that_names_neither_reads_back_empty(self) -> None:
        """Every teachpoint written before these fields existed takes this path,
        so it has to be indistinguishable from one that opted out."""
        store = _store()
        await store.add(_tp())

        loaded = await store.get("hotel_3")

        assert loaded is not None
        assert loaded.taught_with is None
        assert loaded.by_labware == {}

    async def test_only_the_named_fields_are_stored_per_labware(self) -> None:
        """A stored override that filled in the untouched fields as nulls would
        stop the layers underneath reaching this labware at this position."""
        store = _store()
        await store.add(_tp(by_labware={"deep_well": MoveParameterPatch(clearance=45.0)}))

        loaded = await store.get("hotel_3")

        assert loaded is not None
        assert loaded.by_labware["deep_well"].model_dump(exclude_none=True) == {
            "clearance": 45.0,
        }

    async def test_an_update_replaces_the_overrides(self) -> None:
        store = _store()
        await store.add(_tp(by_labware={"deep_well": MoveParameterPatch(clearance=45.0)}))

        await store.update(_tp(by_labware={"tip_box": MoveParameterPatch(z_offset=1.0)}))
        loaded = await store.get("hotel_3")

        assert loaded is not None
        assert list(loaded.by_labware) == ["tip_box"]


class TestTheWireBuilderCarriesThem:
    async def test_build_teachpoint_accepts_both(self) -> None:
        """The REST/CLI write path goes through this builder, so a field it
        drops is a field an operator cannot set however good the DTO is."""
        built = build_teachpoint(
            coord_type="cartesian",
            position_id="hotel_3",
            coords={"x": 1.0, "y": 2.0, "z": 3.0},
            access=None,
            orientation="left",
            gateway=None,
            taught_with="costar_96",
            by_labware={"deep_well": {"clearance": 45.0}},
        )

        assert built.taught_with == "costar_96"
        assert built.by_labware["deep_well"] == MoveParameterPatch(clearance=45.0)

    async def test_build_teachpoint_without_them_is_unchanged(self) -> None:
        built = build_teachpoint(
            coord_type="cartesian",
            position_id="hotel_3",
            coords={"x": 1.0, "y": 2.0, "z": 3.0},
            access=None,
            orientation="left",
            gateway=None,
        )

        assert built.taught_with is None
        assert built.by_labware == {}
