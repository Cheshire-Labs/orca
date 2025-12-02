import pytest
import json
from cheshire_drivers import (
    Teachpoint,
    CartesianCoordinates,
    JointCoordinates,
    TeachpointsRegistry,
    PLRTransporterBackendWrapper,
)


def make_tp(name: str, gateway: str | None = None) -> Teachpoint:
    """Helper to create simple joint-space teachpoint for testing."""
    coords = JointCoordinates(j1=0.0, j2=170.0, j3=0.0, j4=150.0, j5=0.0)
    return Teachpoint(name, coords, access_type="vertical", gateway=gateway)


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
                {"name": "nest_1", "j1": 0, "j2": 170, "j3": 0, "j4": 150, "j5": 0, "gateway": "safe_zone"},
                {"name": "safe_zone", "j1": 0, "j2": 180, "j3": 5, "j4": 160, "j5": 10}
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

        nest_1_data = next(tp for tp in data["teachpoints"] if tp["name"] == "nest_1")
        safe_zone_data = next(tp for tp in data["teachpoints"] if tp["name"] == "safe_zone")

        assert nest_1_data.get("gateway") == "safe_zone"
        assert "gateway" not in safe_zone_data  # None gateways not serialized


class TestGatewayResolution:
    """Test gateway path resolution logic."""

    def test_no_gateway_returns_empty_path(self):
        """Teachpoint without gateway should resolve to empty path."""
        from unittest.mock import MagicMock

        wrapper = PLRTransporterBackendWrapper(MagicMock())
        wrapper.load_teachpoints([make_tp("nest_1")])

        tp = wrapper._teachpoints.get("nest_1")
        path = wrapper._resolve_gateway_path(tp)

        assert path == []

    def test_single_gateway_returns_one_waypoint(self):
        """Teachpoint with single gateway should return path with one waypoint."""
        from unittest.mock import MagicMock

        wrapper = PLRTransporterBackendWrapper(MagicMock())
        wrapper.load_teachpoints([
            make_tp("nest_1", gateway="safe_zone"),
            make_tp("safe_zone"),
        ])

        tp = wrapper._teachpoints.get("nest_1")
        path = wrapper._resolve_gateway_path(tp)

        assert len(path) == 1
        assert path[0].name == "safe_zone"

    def test_chained_gateways_returns_correct_order(self):
        """Gateway chain A->B->C should traverse C then B to reach A."""
        from unittest.mock import MagicMock

        wrapper = PLRTransporterBackendWrapper(MagicMock())
        wrapper.load_teachpoints([
            make_tp("nest_1", gateway="mid_zone"),
            make_tp("mid_zone", gateway="safe_zone"),
            make_tp("safe_zone"),
        ])

        tp = wrapper._teachpoints.get("nest_1")
        path = wrapper._resolve_gateway_path(tp)

        # Should be [safe_zone, mid_zone] - outermost first
        assert len(path) == 2
        assert path[0].name == "safe_zone"
        assert path[1].name == "mid_zone"

    def test_circular_gateway_raises_error(self):
        """Circular gateway reference should raise ValueError."""
        from unittest.mock import MagicMock

        wrapper = PLRTransporterBackendWrapper(MagicMock())
        wrapper.load_teachpoints([
            make_tp("nest_1", gateway="zone_a"),
            make_tp("zone_a", gateway="zone_b"),
            make_tp("zone_b", gateway="zone_a"),  # Circular!
        ])

        tp = wrapper._teachpoints.get("nest_1")

        with pytest.raises(ValueError, match="Circular gateway reference"):
            wrapper._resolve_gateway_path(tp)

    def test_missing_gateway_raises_error(self):
        """Reference to non-existent gateway should raise ValueError."""
        from unittest.mock import MagicMock

        wrapper = PLRTransporterBackendWrapper(MagicMock())
        wrapper.load_teachpoints([
            make_tp("nest_1", gateway="nonexistent"),
        ])

        tp = wrapper._teachpoints.get("nest_1")

        with pytest.raises(ValueError, match="not found in teachpoints"):
            wrapper._resolve_gateway_path(tp)


class TestTeachpointToPlrAccess:
    """Test _teachpoint_to_plr_access() conversion logic."""

    def test_vertical_access_creates_vertical_pattern(self):
        """Vertical access teachpoint should create VerticalAccess pattern."""
        from unittest.mock import MagicMock
        from pylabrobot.arms.backend import VerticalAccess

        wrapper = PLRTransporterBackendWrapper(MagicMock())

        # Use Cartesian with required orientation
        coords = CartesianCoordinates(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        tp = Teachpoint(
            name="test_vertical",
            coordinates=coords,
            orientation="right",
            access_type="vertical",
            gripper_offset=25.0,
            retract_distance=150.0,
        )

        access = wrapper._teachpoint_to_plr_access(tp)

        assert isinstance(access, VerticalAccess)
        assert access.gripper_offset_mm == 25.0
        assert access.approach_height_mm == 150.0

    def test_horizontal_access_creates_horizontal_pattern(self):
        """Horizontal access teachpoint should create HorizontalAccess pattern."""
        from unittest.mock import MagicMock
        from pylabrobot.arms.backend import HorizontalAccess

        wrapper = PLRTransporterBackendWrapper(MagicMock())

        # Use Cartesian with required orientation
        coords = CartesianCoordinates(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        tp = Teachpoint(
            name="test_horizontal",
            coordinates=coords,
            orientation="left",
            access_type="horizontal",
            gripper_offset=20.0,
            vertical_clearance=80.0,
            z_above=15.0,
        )

        access = wrapper._teachpoint_to_plr_access(tp)

        assert isinstance(access, HorizontalAccess)
        assert access.gripper_offset_mm == 20.0
        assert access.approach_distance_mm == 80.0
        assert access.lift_height_mm == 15.0

    def test_invalid_access_type_raises_error(self):
        """Invalid access_type should raise ValueError."""
        from unittest.mock import MagicMock

        wrapper = PLRTransporterBackendWrapper(MagicMock())

        # Use joint coords to bypass orientation check, since we're testing access_type
        coords = JointCoordinates(j1=0.0, j2=170.0, j3=0.0, j4=150.0, j5=0.0)
        tp = Teachpoint(
            name="test_invalid",
            coordinates=coords,
            access_type="diagonal",  # Invalid
            gripper_offset=20.0,
        )

        with pytest.raises(ValueError, match="Invalid access_type"):
            wrapper._teachpoint_to_plr_access(tp)


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
                    "retract_distance": 120.0,
                    "vertical_clearance": 75.0,
                    "z_above": 12.0
                }
            },
            "teachpoints": [
                {"name": "nest_1", "j1": 0, "j2": 170, "j3": 5, "j4": 150, "j5": 10, "access": "nest_access"},
                {"name": "nest_2", "j1": 0, "j2": 180, "j3": 5, "j4": 160, "j5": 10, "access": "nest_access"},
                {"name": "safe_zone", "j1": 0, "j2": 190, "j3": 0, "j4": 180, "j5": 0}
            ]
        }
        """
        json_file = tmp_path / "teachpoints.json"
        json_file.write_text(json_content)

        teachpoints = Teachpoint.load_teachpoints_from_file(str(json_file))

        # Both nest teachpoints should have the custom access config
        nest_1 = next(tp for tp in teachpoints if tp.name == "nest_1")
        nest_2 = next(tp for tp in teachpoints if tp.name == "nest_2")
        safe_zone = next(tp for tp in teachpoints if tp.name == "safe_zone")

        # Custom config values
        assert nest_1.access_type == "horizontal"
        assert nest_1.gripper_offset == 30.0
        assert nest_1.vertical_clearance == 75.0
        assert nest_1.z_above == 12.0

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
                {"name": "nest_1", "j1": 0, "j2": 170, "j3": 0, "j4": 150, "j5": 0, "access": "nonexistent_config"}
            ]
        }
        """
        json_file = tmp_path / "teachpoints.json"
        json_file.write_text(json_content)

        with pytest.raises(ValueError, match="unknown access config"):
            Teachpoint.load_teachpoints_from_file(str(json_file))
