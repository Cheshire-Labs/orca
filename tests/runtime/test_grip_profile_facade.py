"""The operator surface over how each labware type is held."""

import pytest
from cheshire_drivers.move_parameters import MoveParameterPatch

from orca.runtime.danger import ConfirmationRequired
from orca.runtime.db import create_memory_engine
from orca.runtime.facades.grip_profiles import GripProfileFacade
from orca.runtime.grip_profile_service import GripProfileService
from orca.runtime.sqlite_grip_profile_store import SqliteGripProfileStore


def _facade() -> GripProfileFacade:
    return GripProfileFacade(
        GripProfileService(SqliteGripProfileStore(create_memory_engine()))
    )


class TestReading:
    @pytest.mark.asyncio
    async def test_a_type_nobody_measured_reads_as_an_empty_profile(self) -> None:
        """Not an error and not a row. An operator asking about a labware they
        have not tuned should be told exactly that."""
        profile = await _facade().get("never_measured")

        assert profile.labware_type == "never_measured"
        assert profile.patch == MoveParameterPatch()

    @pytest.mark.asyncio
    async def test_reading_does_not_create_a_row(self) -> None:
        """Seeding on read would destroy the difference between a width somebody
        chose and one nobody has ever looked at."""
        facade = _facade()

        await facade.get("never_measured")

        assert await facade.list() == []

    @pytest.mark.asyncio
    async def test_the_list_holds_only_the_types_somebody_measured(self) -> None:
        """A catalog runs to hundreds of types. Listing all of them would bury
        the two that carry a correction."""
        facade = _facade()
        await facade.apply("costar_96", MoveParameterPatch(resource_width=76.0), confirm=True)

        listed = await facade.list()

        assert [p.labware_type for p in listed] == ["costar_96"]


class TestEditing:
    @pytest.mark.asyncio
    async def test_an_edit_merges_into_what_is_already_stored(self) -> None:
        """Correcting the grip height must not wipe the width measured last week."""
        facade = _facade()
        await facade.apply("costar_96", MoveParameterPatch(resource_width=76.0), confirm=True)

        profile = await facade.apply(
            "costar_96", MoveParameterPatch(z_offset=2.5), confirm=True,
        )

        assert profile.patch == MoveParameterPatch(resource_width=76.0, z_offset=2.5)

    @pytest.mark.asyncio
    async def test_clearing_hands_one_field_back_without_touching_the_others(self) -> None:
        facade = _facade()
        await facade.apply(
            "costar_96",
            MoveParameterPatch(resource_width=76.0, z_offset=2.5),
            confirm=True,
        )

        profile = await facade.apply(
            "costar_96", MoveParameterPatch(), clear=["z_offset"], confirm=True,
        )

        assert profile.patch == MoveParameterPatch(resource_width=76.0)

    @pytest.mark.asyncio
    async def test_setting_and_clearing_happen_in_one_write(self) -> None:
        """Two writes would leave a move in between them resolving against a
        mixture neither call intended."""
        facade = _facade()
        await facade.apply(
            "costar_96",
            MoveParameterPatch(resource_width=76.0, z_offset=2.5),
            confirm=True,
        )

        profile = await facade.apply(
            "costar_96",
            MoveParameterPatch(resource_width=80.0),
            clear=["z_offset"],
            confirm=True,
        )

        assert profile.patch == MoveParameterPatch(resource_width=80.0)

    @pytest.mark.asyncio
    async def test_clearing_the_last_field_removes_the_profile_entirely(self) -> None:
        """"No opinion" gets one representation, not two. An empty row that
        still lists would read as a type somebody had configured."""
        facade = _facade()
        await facade.apply("costar_96", MoveParameterPatch(resource_width=76.0), confirm=True)

        await facade.apply(
            "costar_96", MoveParameterPatch(), clear=["resource_width"], confirm=True,
        )

        assert await facade.list() == []


class TestTheConfirmationGate:
    @pytest.mark.asyncio
    async def test_an_unconfirmed_edit_is_refused(self) -> None:
        """Changing a grip width changes how every queued move holds this
        labware. That is a physical consequence, so it asks first."""
        with pytest.raises(ConfirmationRequired):
            await _facade().apply("costar_96", MoveParameterPatch(resource_width=76.0))

    @pytest.mark.asyncio
    async def test_a_refused_edit_writes_nothing(self) -> None:
        facade = _facade()

        with pytest.raises(ConfirmationRequired):
            await facade.apply("costar_96", MoveParameterPatch(resource_width=76.0))

        assert await facade.list() == []

    @pytest.mark.asyncio
    async def test_an_unconfirmed_reset_is_refused(self) -> None:
        with pytest.raises(ConfirmationRequired):
            await _facade().reset("costar_96")


class TestResetting:
    @pytest.mark.asyncio
    async def test_reset_discards_everything_measured_for_the_type(self) -> None:
        facade = _facade()
        await facade.apply("costar_96", MoveParameterPatch(resource_width=76.0), confirm=True)

        assert await facade.reset("costar_96", confirm=True) is True
        assert (await facade.get("costar_96")).patch == MoveParameterPatch()

    @pytest.mark.asyncio
    async def test_resetting_a_type_that_has_no_profile_says_so(self) -> None:
        assert await _facade().reset("never_measured", confirm=True) is False


@pytest.mark.asyncio
async def test_a_number_the_position_decides_is_refused_not_stored() -> None:
    """A grip profile resolves before the site does, so an approach number stored
    on a labware is overwritten on every move. Taking it silently is what sends an
    operator round the loop of raising a clearance that never applies."""
    facade = _facade()

    for field, value in (
        ("access_type", "horizontal"), ("clearance", 45.0),
        ("z_above", 5.0), ("grasp_offset", 3.0),
    ):
        with pytest.raises(ValueError, match="access config"):
            await facade.apply(
                "costar_96", MoveParameterPatch.model_validate({field: value}),
                confirm=True,
            )


@pytest.mark.asyncio
async def test_what_the_labware_itself_says_is_still_taken() -> None:
    """The refusal is about the four approach fields, not about the layer: how a
    labware is gripped and how tall it is are exactly what belongs here."""
    facade = _facade()

    stored = await facade.apply(
        "costar_96",
        MoveParameterPatch(resource_width=76.0, resource_height=43.5, z_offset=2.5),
        confirm=True,
    )

    assert stored.patch.resource_height == 43.5
