"""The operator surface over a position's per-labware exceptions.

Layer three of the move-parameter model has an engine and a persistence half
already; these pin the half an operator actually touches. Without it the only
way to except one plate at one nest is to re-author the topology.
"""

import pytest
from cheshire_drivers.move_parameters import MoveParameterPatch
from cheshire_drivers.teachpoints import (
    AccessConfig,
    CartesianCoordinates,
    Teachpoint,
)

from orca.resource_models.transporter import Transporter
from orca.runtime.danger import ConfirmationRequired
from orca.runtime.facades.teachpoints import TeachpointFacade
from orca.runtime.teachpoint_service import seeded_teachpoint_service


def _teachpoint(taught_with: str | None = None) -> Teachpoint:
    return Teachpoint(
        position_id="hotel_3",
        coordinates=CartesianCoordinates(
            x=1.0, y=2.0, z=3.0, yaw=0.0, pitch=90.0, roll=-180.0,
        ),
        orientation="left",
        access=AccessConfig(
            name="taught_vertical",
            access_type="vertical",
            gripper_offset=20.0,
            vertical_clearance=20.0,
            horizontal_clearance=100.0,
        ),
        taught_with=taught_with,
    )


class _System:
    """Minimal ISystem stand-in: the facade only reaches for a transporter."""

    def __init__(self, transporter: Transporter) -> None:
        self._transporter = transporter

    def get_transporter(self, name: str) -> Transporter:
        if name != self._transporter.name:
            raise KeyError(name)
        return self._transporter

    @property
    def transporters(self) -> list[Transporter]:
        return [self._transporter]


@pytest.fixture
def facade() -> TeachpointFacade:
    transporter = Transporter(
        name="pf400",
        teachpoint_store=seeded_teachpoint_service([_teachpoint()]),
    )
    return TeachpointFacade(_System(transporter))


class TestPerLabwareOverrides:
    @pytest.mark.asyncio
    async def test_a_position_starts_treating_every_labware_the_same(
        self, facade: TeachpointFacade,
    ) -> None:
        teachpoint = await facade.get("pf400", "hotel_3")

        assert teachpoint is not None
        assert teachpoint.by_labware == {}

    @pytest.mark.asyncio
    async def test_an_override_names_one_labware_at_one_position(
        self, facade: TeachpointFacade,
    ) -> None:
        updated = await facade.apply_labware_override(
            "pf400", "hotel_3", "deep_well",
            MoveParameterPatch(clearance=45.0), confirm=True,
        )

        assert updated.by_labware == {"deep_well": MoveParameterPatch(clearance=45.0)}

    @pytest.mark.asyncio
    async def test_a_second_edit_merges_rather_than_replaces(
        self, facade: TeachpointFacade,
    ) -> None:
        """Correcting the grip height must not drop the clearance measured first."""
        await facade.apply_labware_override(
            "pf400", "hotel_3", "deep_well",
            MoveParameterPatch(clearance=45.0), confirm=True,
        )

        updated = await facade.apply_labware_override(
            "pf400", "hotel_3", "deep_well",
            MoveParameterPatch(z_offset=2.5), confirm=True,
        )

        assert updated.by_labware["deep_well"] == MoveParameterPatch(
            clearance=45.0, z_offset=2.5,
        )

    @pytest.mark.asyncio
    async def test_setting_and_clearing_happen_in_one_write(
        self, facade: TeachpointFacade,
    ) -> None:
        await facade.apply_labware_override(
            "pf400", "hotel_3", "deep_well",
            MoveParameterPatch(clearance=45.0, z_offset=2.5), confirm=True,
        )

        updated = await facade.apply_labware_override(
            "pf400", "hotel_3", "deep_well",
            MoveParameterPatch(clearance=50.0), clear=["z_offset"], confirm=True,
        )

        assert updated.by_labware["deep_well"] == MoveParameterPatch(clearance=50.0)

    @pytest.mark.asyncio
    async def test_an_override_edited_down_to_nothing_is_dropped(
        self, facade: TeachpointFacade,
    ) -> None:
        """No exception here gets one representation, not two: an empty patch
        left behind would read as a labware somebody had tuned."""
        await facade.apply_labware_override(
            "pf400", "hotel_3", "deep_well",
            MoveParameterPatch(clearance=45.0), confirm=True,
        )

        updated = await facade.apply_labware_override(
            "pf400", "hotel_3", "deep_well",
            MoveParameterPatch(), clear=["clearance"], confirm=True,
        )

        assert updated.by_labware == {}

    @pytest.mark.asyncio
    async def test_one_labware_exception_does_not_disturb_another(
        self, facade: TeachpointFacade,
    ) -> None:
        await facade.apply_labware_override(
            "pf400", "hotel_3", "deep_well",
            MoveParameterPatch(clearance=45.0), confirm=True,
        )

        updated = await facade.apply_labware_override(
            "pf400", "hotel_3", "lidded_stack",
            MoveParameterPatch(resource_height=30.0), confirm=True,
        )

        assert set(updated.by_labware) == {"deep_well", "lidded_stack"}

    @pytest.mark.asyncio
    async def test_clearing_returns_the_labware_to_how_the_position_works(
        self, facade: TeachpointFacade,
    ) -> None:
        await facade.apply_labware_override(
            "pf400", "hotel_3", "deep_well",
            MoveParameterPatch(clearance=45.0), confirm=True,
        )

        assert await facade.clear_labware_override(
            "pf400", "hotel_3", "deep_well", confirm=True,
        ) is True
        teachpoint = await facade.get("pf400", "hotel_3")
        assert teachpoint is not None and teachpoint.by_labware == {}

    @pytest.mark.asyncio
    async def test_clearing_a_labware_with_no_exception_says_so(
        self, facade: TeachpointFacade,
    ) -> None:
        assert await facade.clear_labware_override(
            "pf400", "hotel_3", "never_excepted", confirm=True,
        ) is False

    @pytest.mark.asyncio
    async def test_an_unknown_position_is_a_lookup_error(
        self, facade: TeachpointFacade,
    ) -> None:
        with pytest.raises(KeyError):
            await facade.apply_labware_override(
                "pf400", "nowhere", "deep_well",
                MoveParameterPatch(clearance=45.0), confirm=True,
            )


class TestTaughtWith:
    @pytest.mark.asyncio
    async def test_naming_what_a_position_was_taught_with(
        self, facade: TeachpointFacade,
    ) -> None:
        updated = await facade.set_taught_with(
            "pf400", "hotel_3", "costar_96", confirm=True,
        )

        assert updated.taught_with == "costar_96"

    @pytest.mark.asyncio
    async def test_it_can_be_unsaid(self, facade: TeachpointFacade) -> None:
        """A position re-jogged with nothing recorded must be able to say so,
        rather than keep pointing at labware nobody taught it with."""
        await facade.set_taught_with("pf400", "hotel_3", "costar_96", confirm=True)

        updated = await facade.set_taught_with("pf400", "hotel_3", None, confirm=True)

        assert updated.taught_with is None


class TestTheConfirmationGate:
    @pytest.mark.asyncio
    async def test_an_unconfirmed_override_is_refused(
        self, facade: TeachpointFacade,
    ) -> None:
        """This is the narrowest layer: it wins over everything, so it asks."""
        with pytest.raises(ConfirmationRequired):
            await facade.apply_labware_override(
                "pf400", "hotel_3", "deep_well", MoveParameterPatch(clearance=45.0),
            )

    @pytest.mark.asyncio
    async def test_a_refused_override_writes_nothing(
        self, facade: TeachpointFacade,
    ) -> None:
        with pytest.raises(ConfirmationRequired):
            await facade.apply_labware_override(
                "pf400", "hotel_3", "deep_well", MoveParameterPatch(clearance=45.0),
            )

        teachpoint = await facade.get("pf400", "hotel_3")
        assert teachpoint is not None and teachpoint.by_labware == {}

    @pytest.mark.asyncio
    async def test_an_unconfirmed_clear_is_refused(
        self, facade: TeachpointFacade,
    ) -> None:
        with pytest.raises(ConfirmationRequired):
            await facade.clear_labware_override("pf400", "hotel_3", "deep_well")

    @pytest.mark.asyncio
    async def test_an_unconfirmed_taught_with_is_refused(
        self, facade: TeachpointFacade,
    ) -> None:
        with pytest.raises(ConfirmationRequired):
            await facade.set_taught_with("pf400", "hotel_3", "costar_96")
