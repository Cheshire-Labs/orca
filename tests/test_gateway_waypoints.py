import pytest
import json
from orca.resource_models.resource_extras.teachpoints import (
    Teachpoint,
    CartesianCoordinates,
    TeachpointsRegistry,
)


def make_tp(name: str, gateway: str | None = None) -> Teachpoint:
    """Helper to create simple teachpoint for testing."""
    coords = CartesianCoordinates(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    return Teachpoint(name, coords, None, "vertical", gateway=gateway)


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
                {"name": "nest_1", "x": 0, "y": 0, "z": 0, "yaw": 0, "pitch": 0, "roll": 0, "gateway": "safe_zone"},
                {"name": "safe_zone", "x": 0, "y": 0, "z": 100, "yaw": 0, "pitch": 0, "roll": 0}
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
        from orca.driver_management.drivers.plr_wrappers import PLRTransporterBackendWrapper
        from unittest.mock import MagicMock

        wrapper = PLRTransporterBackendWrapper(MagicMock())
        wrapper.load_teachpoints([make_tp("nest_1")])

        tp = wrapper._teachpoints.get("nest_1")
        path = wrapper._resolve_gateway_path(tp)

        assert path == []

    def test_single_gateway_returns_one_waypoint(self):
        """Teachpoint with single gateway should return path with one waypoint."""
        from orca.driver_management.drivers.plr_wrappers import PLRTransporterBackendWrapper
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
        from orca.driver_management.drivers.plr_wrappers import PLRTransporterBackendWrapper
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
        from orca.driver_management.drivers.plr_wrappers import PLRTransporterBackendWrapper
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
        from orca.driver_management.drivers.plr_wrappers import PLRTransporterBackendWrapper
        from unittest.mock import MagicMock

        wrapper = PLRTransporterBackendWrapper(MagicMock())
        wrapper.load_teachpoints([
            make_tp("nest_1", gateway="nonexistent"),
        ])

        tp = wrapper._teachpoints.get("nest_1")

        with pytest.raises(ValueError, match="not found in teachpoints"):
            wrapper._resolve_gateway_path(tp)
