"""Layering the scalars a move gets: the seed, the arm's defaults, then the site."""

import pytest
from cheshire_drivers.move_parameters import SEED_MOVE_PARAMETERS, MoveParameterPatch
from cheshire_drivers.teachpoints import AccessConfig, CartesianCoordinates, Teachpoint
from pydantic import ValidationError

from orca.runtime.move_parameters import resolve_move_parameters
from orca.runtime.move_parameter_models import MoveDefaultsPatchRequest


def _teachpoint(access: AccessConfig | None = None) -> Teachpoint:
    """A taught position. Defaults to a plain vertical nest, which is what almost
    every position on a deck is."""
    if access is None:
        access = _vertical()
    return Teachpoint(
        position_id="pad_1",
        coordinates=CartesianCoordinates(x=1.0, y=2.0, z=3.0, yaw=0.0, pitch=90.0, roll=-180.0),
        orientation="left",
        access=access,
    )


def _vertical(
    gripper_offset: float = 20.0,
    vertical_clearance: float = 20.0,
) -> AccessConfig:
    return AccessConfig(
        name="taught_vertical",
        access_type="vertical",
        gripper_offset=gripper_offset,
        vertical_clearance=vertical_clearance,
        horizontal_clearance=100.0,
    )


class TestNothingConfigured:
    def test_a_plain_nest_gets_the_defaults_unchanged(self) -> None:
        """Layer one has to stand on its own, or nothing can be authored at all.
        The seed's approach numbers ARE the built-in vertical config's, so a
        position taught with those changes nothing."""
        resolved = resolve_move_parameters(MoveParameterPatch(), _teachpoint())

        assert resolved.parameters == SEED_MOVE_PARAMETERS

    def test_a_position_that_names_no_approach_is_reached_from_above(self) -> None:
        """The fallback the rest of the stack already declares, finally applied.
        A bare taught position is an open nest, which is what almost all of them are."""
        bare = Teachpoint(
            position_id="pad_1",
            coordinates=CartesianCoordinates(
                x=1.0, y=2.0, z=3.0, yaw=0.0, pitch=90.0, roll=-180.0,
            ),
            orientation="left",
        )

        resolved = resolve_move_parameters(MoveParameterPatch(), bare)

        assert resolved.parameters.access_type == "vertical"
        assert resolved.parameters.z_above == 0.0

    def test_every_field_is_credited_to_the_layer_that_set_it(self) -> None:
        """A site that happens to agree with the defaults still SET those fields,
        and the record says so. Crediting by value rather than by who wrote it
        would make the explanation lie the moment two layers agree."""
        resolved = resolve_move_parameters(MoveParameterPatch(), _teachpoint())

        assert set(resolved.sources) == set(SEED_MOVE_PARAMETERS.model_dump())
        assert resolved.sources["access_type"] == "site"
        assert resolved.sources["clearance"] == "site"
        assert resolved.sources["resource_width"] == "seed"
        assert resolved.sources["jaw_opening"] == "seed"


class TestTheSiteNarrowsTheDefaults:
    def test_a_vertical_site_sets_its_clearance_and_gripper_offset(self) -> None:
        resolved = resolve_move_parameters(
            MoveParameterPatch(),
            _teachpoint(_vertical(vertical_clearance=45.0, gripper_offset=3.0)),
        )

        assert resolved.parameters.access_type == "vertical"
        assert resolved.parameters.clearance == 45.0
        assert resolved.parameters.grasp_offset == 3.0

    def test_a_vertical_site_does_not_lift_after_a_retract_it_never_makes(self) -> None:
        resolved = resolve_move_parameters(
            MoveParameterPatch(), _teachpoint(_vertical(vertical_clearance=45.0)),
        )

        assert resolved.parameters.z_above == 0.0

    def test_a_horizontal_site_backs_out_sideways_then_lifts(self) -> None:
        """Two different numbers on one config: out of the slot, then up."""
        resolved = resolve_move_parameters(
            MoveParameterPatch(),
            _teachpoint(
                AccessConfig(
                    name="hotel", access_type="horizontal",
                    gripper_offset=8.0, vertical_clearance=12.0, horizontal_clearance=60.0,
                )
            ),
        )

        assert resolved.parameters.access_type == "horizontal"
        assert resolved.parameters.clearance == 60.0
        assert resolved.parameters.z_above == 12.0

    def test_the_site_leaves_everything_it_says_nothing_about_alone(self) -> None:
        resolved = resolve_move_parameters(
            MoveParameterPatch(), _teachpoint(_vertical(vertical_clearance=45.0)),
        )

        assert resolved.parameters.resource_width == SEED_MOVE_PARAMETERS.resource_width
        assert resolved.parameters.jaw_opening == SEED_MOVE_PARAMETERS.jaw_opening
        assert resolved.parameters.speed is None

    def test_the_site_is_credited_only_for_the_fields_it_set(self) -> None:
        """Reading back why a plate moved the way it did is the point of this."""
        resolved = resolve_move_parameters(
            MoveParameterPatch(), _teachpoint(_vertical(vertical_clearance=45.0)),
        )

        assert resolved.sources["clearance"] == "site"
        assert resolved.sources["access_type"] == "site"
        assert resolved.sources["resource_width"] == "seed"


class TestTheDefaultsAreOperatorSet:
    def test_changing_the_defaults_row_changes_every_unconfigured_move(self) -> None:
        resolved = resolve_move_parameters(
            MoveParameterPatch(travel_margin=25.0), _teachpoint(),
        )

        assert resolved.parameters.travel_margin == 25.0
        assert resolved.sources["travel_margin"] == "defaults"

    def test_a_field_the_deployment_never_tuned_is_credited_to_the_seed(self) -> None:
        """An operator's number and a built-in nobody has looked at must not read
        the same, or "is this arm calibrated" has no answer."""
        resolved = resolve_move_parameters(
            MoveParameterPatch(travel_margin=25.0), _teachpoint(),
        )

        assert resolved.sources["travel_margin"] == "defaults"
        assert resolved.sources["jaw_opening"] == "seed"
        assert resolved.parameters.jaw_opening == SEED_MOVE_PARAMETERS.jaw_opening


class TestTheEditEnvelope:
    def test_an_edit_naming_nothing_is_refused(self) -> None:
        """Accepting it answers 200 with an unchanged record, which reads exactly
        like the edit having been applied."""
        with pytest.raises(ValidationError):
            MoveDefaultsPatchRequest.model_validate({})

    def test_a_body_with_the_wrong_outer_key_is_refused(self) -> None:
        """`{"parameters": {...}}` would otherwise validate to an empty edit and
        report success over an arm whose numbers never changed."""
        with pytest.raises(ValidationError):
            MoveDefaultsPatchRequest.model_validate(
                {"parameters": {"travel_margin": 25.0}},
            )

    def test_an_edit_that_only_clears_is_accepted(self) -> None:
        body = MoveDefaultsPatchRequest.model_validate({"clear": ["speed"]})

        assert body.clear == ["speed"]


class TestAMalformedSite:
    def test_an_access_type_the_arm_does_not_know_is_refused_here(self) -> None:
        """Not at the driver: by then the layering has already been thrown away."""
        teachpoint = _teachpoint(_vertical())
        teachpoint.access_type = "diagonal"

        with pytest.raises(ValueError, match="diagonal"):
            resolve_move_parameters(MoveParameterPatch(), teachpoint)
