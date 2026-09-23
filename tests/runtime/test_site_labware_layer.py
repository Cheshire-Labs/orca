"""This labware, at this one position: the narrowest layer there is."""

from cheshire_drivers.move_parameters import MoveParameterPatch
from cheshire_drivers.teachpoints import AccessConfig, CartesianCoordinates, Teachpoint

from orca.runtime.move_parameters import resolve_move_parameters


def _teachpoint(
    clearance: float = 20.0,
    by_labware: dict[str, MoveParameterPatch] | None = None,
    taught_with: str | None = None,
) -> Teachpoint:
    return Teachpoint(
        position_id="hotel_3",
        coordinates=CartesianCoordinates(x=1.0, y=2.0, z=3.0, yaw=0.0, pitch=90.0, roll=-180.0),
        orientation="left",
        access=AccessConfig(
            name="taught_vertical",
            access_type="vertical",
            gripper_offset=20.0,
            vertical_clearance=clearance,
            horizontal_clearance=100.0,
        ),
        taught_with=taught_with,
        by_labware=by_labware,
    )


class TestTheNarrowestLayerWins:
    def test_one_plate_gets_an_exception_at_one_nest(self) -> None:
        """The case the layer exists for: a nest that suits everything except
        one deep-well plate, which needs more clearance here and nowhere else."""
        resolved = resolve_move_parameters(
            MoveParameterPatch(),
            _teachpoint(by_labware={"deep_well": MoveParameterPatch(clearance=45.0)}),
            None,
            "deep_well",
        )

        assert resolved.parameters.clearance == 45.0
        assert resolved.sources["clearance"] == "site_labware"

    def test_it_beats_the_site_itself(self) -> None:
        """Both name clearance. "This labware here" is narrower than "here"."""
        resolved = resolve_move_parameters(
            MoveParameterPatch(),
            _teachpoint(
                clearance=30.0,
                by_labware={"deep_well": MoveParameterPatch(clearance=45.0)},
            ),
            None,
            "deep_well",
        )

        assert resolved.parameters.clearance == 45.0

    def test_it_beats_the_labware_s_own_profile(self) -> None:
        """The plate asks for 5 everywhere; this nest overrides it to 45 here."""
        resolved = resolve_move_parameters(
            MoveParameterPatch(),
            _teachpoint(by_labware={"deep_well": MoveParameterPatch(z_offset=4.0)}),
            MoveParameterPatch(z_offset=1.0),
            "deep_well",
        )

        assert resolved.parameters.z_offset == 4.0
        assert resolved.sources["z_offset"] == "site_labware"


class TestItAppliesToTheRightLabwareOnly:
    def test_a_different_labware_at_the_same_nest_is_untouched(self) -> None:
        """An override keyed to the wrong labware silently applying to every
        move through that nest is the failure this guards."""
        resolved = resolve_move_parameters(
            MoveParameterPatch(),
            _teachpoint(
                clearance=30.0,
                by_labware={"deep_well": MoveParameterPatch(clearance=45.0)},
            ),
            None,
            "costar_96",
        )

        assert resolved.parameters.clearance == 30.0
        assert resolved.sources["clearance"] == "site"

    def test_a_move_carrying_nothing_takes_no_override(self) -> None:
        """A reposition with an empty gripper must not pick up a plate's override."""
        resolved = resolve_move_parameters(
            MoveParameterPatch(),
            _teachpoint(
                clearance=30.0,
                by_labware={"deep_well": MoveParameterPatch(clearance=45.0)},
            ),
        )

        assert resolved.parameters.clearance == 30.0

    def test_a_nest_with_no_overrides_resolves_as_before(self) -> None:
        with_none = resolve_move_parameters(
            MoveParameterPatch(), _teachpoint(), None, "costar_96",
        )
        without_labware = resolve_move_parameters(MoveParameterPatch(), _teachpoint())

        assert with_none.parameters == without_labware.parameters
        assert with_none.sources == without_labware.sources


class TestWhatTaughtWithDoesAndDoesNot:
    def test_it_rides_on_the_teachpoint_without_changing_any_number(self) -> None:
        """Recording what a position was taught on gives `z_offset` something to
        be relative to. It must not itself move the arm: whether a taller
        labware grips higher depends on where the jaws close, which no catalog
        number answers."""
        taught = resolve_move_parameters(
            MoveParameterPatch(), _teachpoint(taught_with="costar_96"), None, "deep_well",
        )
        untaught = resolve_move_parameters(
            MoveParameterPatch(), _teachpoint(), None, "deep_well",
        )

        assert taught.parameters == untaught.parameters

    def test_it_survives_the_wire(self) -> None:
        """The operator surfaces and the driver payload both carry it, so a
        position keeps knowing what it was taught on across a round trip."""
        teachpoint = _teachpoint(
            taught_with="costar_96",
            by_labware={"deep_well": MoveParameterPatch(clearance=45.0)},
        )

        restored = Teachpoint.from_dict(teachpoint.to_dict())

        assert restored.taught_with == "costar_96"
        assert restored.by_labware["deep_well"] == MoveParameterPatch(clearance=45.0)
