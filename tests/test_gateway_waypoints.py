import pytest
import json
from cheshire_drivers import (
    Teachpoint,
    CartesianCoordinates,
    TeachpointsRegistry,
)
from cheshire_drivers.teachpoints import InMemoryTeachpointStore

from orca.runtime.move_parameters import site_patch


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


class TestTeachpointJsonPersistence:
    """Test gateway field in JSON load/save."""

    def test_load_teachpoint_with_gateway(self, tmp_path):
        """Loading JSON with gateway field should set teachpoint.gateway."""
        json_content = """
        {
            "teachpoints": [
                {"position_id": "nest_1", "base": 170, "shoulder": 0, "elbow": 150, "wrist": 0, "gateway": "safe_zone"},
                {"position_id": "safe_zone", "base": 180, "shoulder": 5, "elbow": 160, "wrist": 10}
            ]
        }
        """
        json_file = tmp_path / "teachpoints.json"
        json_file.write_text(json_content)

        teachpoints = Teachpoint.load_teachpoints_from_file(str(json_file))

        assert teachpoints[0].gateway == "safe_zone"
        assert teachpoints[1].gateway is None

    def test_save_teachpoint_with_gateway(self, tmp_path):
        """Saving teachpoint with gateway should persist gateway field."""
        registry = TeachpointsRegistry()
        registry.add(make_tp("nest_1", gateway="safe_zone"))
        registry.add(make_tp("safe_zone"))

        json_file = tmp_path / "teachpoints.json"
        registry.save(str(json_file))

        # Reload and verify
        with open(json_file) as f:
            data = json.load(f)

        nest_1_data = next(tp for tp in data["teachpoints"] if tp["position_id"] == "nest_1")
        safe_zone_data = next(tp for tp in data["teachpoints"] if tp["position_id"] == "safe_zone")

        assert nest_1_data.get("gateway") == "safe_zone"
        assert "gateway" not in safe_zone_data  # None gateways not serialized


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


class TestAccessConfigLoading:
    """Test load_teachpoints_from_file() with access_configs."""

    def test_load_teachpoints_with_access_configs(self, tmp_path):
        """Loading JSON with access_configs should resolve references correctly."""
        json_content = """
        {
            "access_configs": {
                "nest_access": {
                    "access_type": "horizontal",
                    "gripper_offset": 30.0,
                    "horizontal_clearance": 120.0,
                    "vertical_clearance": 75.0
                }
            },
            "teachpoints": [
                {"position_id": "nest_1", "x": 100, "y": 0, "z": 50, "yaw": 180, "pitch": 90, "roll": 0, "orientation": "right", "access": "nest_access"},
                {"position_id": "nest_2", "x": 150, "y": 0, "z": 50, "yaw": 180, "pitch": 90, "roll": 0, "orientation": "right", "access": "nest_access"},
                {"position_id": "safe_zone", "base": 190, "shoulder": 0, "elbow": 180, "wrist": 0}
            ]
        }
        """
        json_file = tmp_path / "teachpoints.json"
        json_file.write_text(json_content)

        teachpoints = Teachpoint.load_teachpoints_from_file(str(json_file))

        # Both nest teachpoints should have the custom access config
        nest_1 = next(tp for tp in teachpoints if tp.position_id == "nest_1")
        nest_2 = next(tp for tp in teachpoints if tp.position_id == "nest_2")
        safe_zone = next(tp for tp in teachpoints if tp.position_id == "safe_zone")

        # Custom config values
        assert nest_1.access_type == "horizontal"
        assert nest_1.gripper_offset == 30.0
        assert nest_1.horizontal_clearance == 120.0
        assert nest_1.vertical_clearance == 75.0

        # Same config shared by both nests
        assert nest_2.access_type == nest_1.access_type
        assert nest_2.gripper_offset == nest_1.gripper_offset

        # Waypoint without access config - should have access_type=None
        assert safe_zone.access_type is None

    def test_load_teachpoints_unknown_access_config_raises_error(self, tmp_path):
        """Reference to non-existent access config should raise ValueError."""
        json_content = """
        {
            "teachpoints": [
                {"position_id": "nest_1", "base": 170, "shoulder": 0, "elbow": 150, "wrist": 0, "access": "nonexistent_config"}
            ]
        }
        """
        json_file = tmp_path / "teachpoints.json"
        json_file.write_text(json_content)

        with pytest.raises(ValueError, match="unknown access config"):
            Teachpoint.load_teachpoints_from_file(str(json_file))
