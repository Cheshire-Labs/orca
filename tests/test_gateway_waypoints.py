import pytest
from cheshire_drivers import (
    Teachpoint,
    CartesianCoordinates,
)

from orca.resource_models.move_parameters import site_patch


def make_tp(name: str, gateway: str | None = None) -> Teachpoint:
    """Helper to create Cartesian teachpoint for testing (access_type requires Cartesian)."""
    coords = CartesianCoordinates(x=100.0, y=0.0, z=50.0, yaw=180.0, pitch=90.0, roll=0.0)
    return Teachpoint(name, coords, orientation="right", access_type="vertical", gateway=gateway)


class TestTeachpointGateway:
    """Test Teachpoint gateway property."""

    def test_teachpoint_gateway_defaults_to_none(self):
        """Teachpoint without gateway arg should have gateway=None."""
        tp = make_tp("test")
        assert tp.gateway is None

    def test_teachpoint_gateway_set_correctly(self):
        """Teachpoint with gateway arg should store it."""
        tp = make_tp("test", gateway="safe_zone")
        assert tp.gateway == "safe_zone"


# `TestGatewayResolution` (formerly here) tested gateway-path resolution
# inside `PLRTransporterBackendWrapper._resolve_gateway_path`. Gateway-path
# resolution moved upstream into orca-core's
# `Transporter._resolve_gateway_path`. The same scenarios (single-hop,
# multi-hop chain, circular reference, missing gateway, missing
# destination) are covered at `tests/test_transporter_resolution.py`
# (`TestGatewayChainResolution` + `TestResolutionFailures`). The wrapper-
# bound test class is removed because the methods it tested no longer
# exist on the driver. Device-gateway handoff (`LabwareStagingBridge`
# + `Device._do_notify_placed`) is a separate concern and is covered by
# `tests/test_device_gateway_deck.py`, which is unaffected.


class TestASiteBecomesMoveParameters:
    """How a taught position narrows a move.

    The vertical-versus-horizontal branch used to live in the driver, reading the
    teachpoint a second time. It resolves here now so the site is one layer among
    several rather than the last word, and so a driver receives numbers.
    """

    def test_a_vertical_site_reaches_the_plate_from_above(self):
        """An open nest is approached straight down, backing off by the taught
        clearance, with the taught grasp offset added while a plate is held."""
        tp = Teachpoint(
            position_id="test_vertical",
            coordinates=CartesianCoordinates(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            orientation="right",
            access_type="vertical",
            gripper_offset=25.0,
            vertical_clearance=150.0,
        )

        patch = site_patch(tp)

        assert patch.access_type == "vertical"
        assert patch.grasp_offset == 25.0
        assert patch.clearance == 150.0

    def test_a_horizontal_site_backs_out_sideways_then_lifts(self):
        """A hotel slot is entered and left sideways, so the taught horizontal
        clearance is the back-off and the vertical one is the lift after it."""
        tp = Teachpoint(
            position_id="test_horizontal",
            coordinates=CartesianCoordinates(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            orientation="left",
            access_type="horizontal",
            gripper_offset=20.0,
            horizontal_clearance=80.0,
            vertical_clearance=15.0,
        )

        patch = site_patch(tp)

        assert patch.access_type == "horizontal"
        assert patch.grasp_offset == 20.0
        assert patch.clearance == 80.0
        assert patch.z_above == 15.0

    def test_a_site_says_nothing_about_the_grip_itself(self):
        """A nest does not know what is being put into it, so the width, the lift
        and the jaws are left to the layers that do."""
        tp = Teachpoint(
            position_id="test_vertical",
            coordinates=CartesianCoordinates(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            orientation="right",
            access_type="vertical",
        )

        contributed = site_patch(tp).model_dump(exclude_none=True)

        assert "resource_width" not in contributed
        assert "resource_height" not in contributed
        assert "jaw_opening" not in contributed

    def test_an_approach_the_arm_has_no_motion_for_is_refused(self):
        tp = Teachpoint(
            position_id="test_invalid",
            coordinates=CartesianCoordinates(
                x=100.0, y=0.0, z=50.0, yaw=180.0, pitch=90.0, roll=0.0,
            ),
            orientation="right",
            access_type="diagonal",
            gripper_offset=20.0,
        )

        with pytest.raises(ValueError, match="diagonal"):
            site_patch(tp)
