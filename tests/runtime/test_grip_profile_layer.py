"""What is being moved changes the move: layer two over the arm's defaults."""

import pytest
from cheshire_drivers.move_parameters import (
    SEED_MOVE_PARAMETERS,
    MoveParameterPatch,
)
from cheshire_drivers.teachpoints import AccessConfig, CartesianCoordinates, Teachpoint

from orca.runtime.db import create_memory_engine
from orca.runtime.grip_profile_service import GripProfileService
from orca.runtime.move_parameters import resolve_move_parameters
from orca.runtime.sqlite_grip_profile_store import SqliteGripProfileStore


def _teachpoint(clearance: float = 20.0) -> Teachpoint:
    return Teachpoint(
        position_id="pad_1",
        coordinates=CartesianCoordinates(x=1.0, y=2.0, z=3.0, yaw=0.0, pitch=90.0, roll=-180.0),
        orientation="left",
        access=AccessConfig(
            name="taught_vertical",
            access_type="vertical",
            gripper_offset=20.0,
            vertical_clearance=clearance,
            horizontal_clearance=100.0,
        ),
    )


def _service() -> GripProfileService:
    return GripProfileService(SqliteGripProfileStore(create_memory_engine()))


class TestTheLabwareNarrowsTheDefaults:
    def test_a_tip_box_grips_at_its_own_width(self) -> None:
        """The number the seed admits it is guessing. A grip width is jaw
        separation on the skirt, which differs per labware type."""
        resolved = resolve_move_parameters(
            MoveParameterPatch(),
            _teachpoint(),
            MoveParameterPatch(resource_width=82.0),
        )

        assert resolved.parameters.resource_width == 82.0
        assert resolved.sources["resource_width"] == "labware"

    def test_a_type_inherits_every_field_it_says_nothing_about(self) -> None:
        """The whole point of a sparse layer: correcting the grip width must not
        freeze the travel margin at today's value."""
        resolved = resolve_move_parameters(
            MoveParameterPatch(),
            _teachpoint(),
            MoveParameterPatch(resource_width=82.0),
        )

        assert resolved.parameters.travel_margin == SEED_MOVE_PARAMETERS.travel_margin
        assert resolved.sources["travel_margin"] == "seed"
        assert resolved.sources["jaw_opening"] == "seed"

    def test_carrying_nothing_resolves_exactly_as_before(self) -> None:
        """A move with no labware (a reposition, an ad-hoc reach) must not
        acquire a labware layer from somewhere."""
        with_labware = resolve_move_parameters(MoveParameterPatch(), _teachpoint(), None)
        before = resolve_move_parameters(MoveParameterPatch(), _teachpoint())

        assert with_labware.parameters == before.parameters
        assert with_labware.sources == before.sources

    def test_an_empty_profile_credits_nothing_to_the_labware(self) -> None:
        """A stored row that names no field is not an opinion. Crediting it
        would make the explanation say the labware decided something it did not."""
        resolved = resolve_move_parameters(
            MoveParameterPatch(), _teachpoint(), MoveParameterPatch(),
        )

        assert set(resolved.sources.values()) == {"seed", "site"}


class TestTheSiteWinsOverTheLabware:
    def test_a_nest_clearance_beats_the_type_s_general_statement(self) -> None:
        """Both layers can name clearance. The site is the narrower claim: it is
        about one place, where the type's is about everywhere it goes."""
        resolved = resolve_move_parameters(
            MoveParameterPatch(),
            _teachpoint(clearance=45.0),
            MoveParameterPatch(clearance=5.0),
        )

        assert resolved.parameters.clearance == 45.0
        assert resolved.sources["clearance"] == "site"

    def test_the_type_still_wins_where_the_site_is_silent(self) -> None:
        resolved = resolve_move_parameters(
            MoveParameterPatch(),
            _teachpoint(clearance=45.0),
            MoveParameterPatch(clearance=5.0, resource_width=82.0),
        )

        assert resolved.parameters.resource_width == 82.0
        assert resolved.sources["resource_width"] == "labware"


class TestTheProfilesArePersisted:
    @pytest.mark.asyncio
    async def test_a_stored_profile_comes_back_as_the_patch_that_was_written(self) -> None:
        service = _service()

        await service.set("tip_box_1000ul", MoveParameterPatch(resource_width=82.0))

        assert await service.get("tip_box_1000ul") == MoveParameterPatch(resource_width=82.0)

    @pytest.mark.asyncio
    async def test_a_type_with_no_profile_reads_as_no_opinion(self) -> None:
        service = _service()

        assert await service.get("never_configured") is None

    @pytest.mark.asyncio
    async def test_only_the_named_fields_are_stored(self) -> None:
        """A row that persisted the unset fields as nulls would still round-trip,
        but it would pin them the moment anything read the row as total."""
        service = _service()

        await service.set("costar_96", MoveParameterPatch(resource_width=80.0))
        stored = await service.get("costar_96")

        assert stored is not None
        assert stored.model_dump(exclude_none=True) == {"resource_width": 80.0}

    @pytest.mark.asyncio
    async def test_setting_a_type_twice_replaces_rather_than_duplicates(self) -> None:
        service = _service()

        await service.set("costar_96", MoveParameterPatch(resource_width=80.0))
        await service.set("costar_96", MoveParameterPatch(resource_width=76.0))

        assert await service.get("costar_96") == MoveParameterPatch(resource_width=76.0)
        assert list(await service.list()) == ["costar_96"]

    @pytest.mark.asyncio
    async def test_seeding_never_overwrites_what_an_operator_measured(self) -> None:
        """A topology re-declaring its starting point on every boot must not undo
        the width someone corrected on the bench."""
        service = _service()
        await service.set("costar_96", MoveParameterPatch(resource_width=76.0))

        await service.seed_if_missing("costar_96", MoveParameterPatch(resource_width=80.0))

        assert await service.get("costar_96") == MoveParameterPatch(resource_width=76.0)

    @pytest.mark.asyncio
    async def test_deleting_a_profile_returns_the_type_to_inheriting(self) -> None:
        service = _service()
        await service.set("costar_96", MoveParameterPatch(resource_width=76.0))

        assert await service.delete("costar_96") is True
        assert await service.get("costar_96") is None

    @pytest.mark.asyncio
    async def test_deleting_a_type_that_has_no_profile_says_so(self) -> None:
        service = _service()

        assert await service.delete("never_configured") is False
