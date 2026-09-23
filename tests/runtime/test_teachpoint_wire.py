"""Unit tests for the shared teachpoint wire-conversion module.

Pins the validate / convert / build behavior relocated from a hosted deployment so both
backends share one copy: cartesian + joint validation, the reject-dupes
guard, coords_to_typed, typed_to_wire round-trip, and build_teachpoint.
"""

import pytest
from cheshire_drivers.teachpoints import (
    AccessConfig,
    CartesianCoordinates,
    JointCoordinates,
    Teachpoint,
)

from orca.runtime.teachpoint_wire import (
    InvalidTeachpointCoordsError,
    build_teachpoint,
    coord_type_for,
    coords_to_typed,
    reject_top_level_dupes_in_coords,
    typed_to_wire,
    validate_cartesian_coords,
    validate_coords,
    validate_joint_coords,
)


_CARTESIAN_WIRE = {"x": 1.0, "y": 2.0, "z": 3.0, "yaw": 0.0, "pitch": 90.0, "roll": 180.0}
_JOINT_WIRE = {
    "rail": 5.0, "base": 10.0, "shoulder": 20.0,
    "elbow": 30.0, "wrist": 40.0, "gripper": 0.0,
}


def _access() -> AccessConfig:
    return AccessConfig(name="ac1", access_type="vertical")


# -- validate_coords (combined wire check) -----------------------------------


def test_validate_coords_cartesian_ok() -> None:
    validate_coords("cartesian", _CARTESIAN_WIRE)


def test_validate_coords_joint_ok() -> None:
    validate_coords("joint", _JOINT_WIRE)


def test_validate_coords_bad_coord_type_raises() -> None:
    with pytest.raises(InvalidTeachpointCoordsError):
        validate_coords("diagonal", _CARTESIAN_WIRE)


def test_validate_coords_rejects_nested_type() -> None:
    with pytest.raises(InvalidTeachpointCoordsError):
        validate_coords("cartesian", {**_CARTESIAN_WIRE, "type": "cartesian"})


def test_validate_coords_rejects_nested_orientation() -> None:
    with pytest.raises(InvalidTeachpointCoordsError):
        validate_coords("cartesian", {**_CARTESIAN_WIRE, "orientation": "left"})


def test_validate_coords_missing_field_raises() -> None:
    with pytest.raises(InvalidTeachpointCoordsError):
        validate_coords("cartesian", {"x": 1.0, "y": 2.0})


# -- low-level validators ----------------------------------------------------


def test_validate_cartesian_unknown_field_raises() -> None:
    with pytest.raises(InvalidTeachpointCoordsError):
        validate_cartesian_coords({"type": "cartesian", "x": 1, "y": 2, "z": 3, "rx": 9})


def test_validate_joint_unknown_field_raises() -> None:
    bad = {"type": "joint", **_JOINT_WIRE, "twist": 1}
    with pytest.raises(InvalidTeachpointCoordsError):
        validate_joint_coords(bad)


def test_validate_cartesian_non_numeric_raises() -> None:
    with pytest.raises(InvalidTeachpointCoordsError):
        validate_cartesian_coords({"type": "cartesian", "x": "nope", "y": 2, "z": 3})


def test_reject_dupes_passes_clean_dict() -> None:
    reject_top_level_dupes_in_coords(_CARTESIAN_WIRE)


# -- conversions -------------------------------------------------------------


def test_coords_to_typed_cartesian() -> None:
    c = coords_to_typed("cartesian", _CARTESIAN_WIRE)
    assert isinstance(c, CartesianCoordinates)
    assert (c.x, c.y, c.z) == (1.0, 2.0, 3.0)


def test_coords_to_typed_joint_defaults_rail_gripper() -> None:
    c = coords_to_typed(
        "joint",
        {"base": 10.0, "shoulder": 20.0, "elbow": 30.0, "wrist": 40.0},
    )
    assert isinstance(c, JointCoordinates)
    assert c.rail == 0.0
    assert c.gripper == 0.0


def test_typed_to_wire_roundtrips_cartesian() -> None:
    c = coords_to_typed("cartesian", _CARTESIAN_WIRE)
    tp = Teachpoint(position_id="p", coordinates=c, orientation="left")
    wire = typed_to_wire(tp)
    assert "type" not in wire
    assert wire["x"] == 1.0
    assert coord_type_for(tp) == "cartesian"


def test_typed_to_wire_empty_for_no_coords() -> None:
    tp = Teachpoint(position_id="p")
    assert typed_to_wire(tp) == {}
    assert coord_type_for(tp) == ""


# -- build_teachpoint --------------------------------------------------------


def test_build_teachpoint_cartesian_with_access() -> None:
    tp = build_teachpoint(
        coord_type="cartesian",
        position_id="slot1",
        coords=_CARTESIAN_WIRE,
        access=_access(),
        orientation="right",
        gateway=None,
    )
    assert tp.position_id == "slot1"
    assert tp.orientation == "right"
    assert tp.access_config_name == "ac1"
    assert tp.is_cartesian()


def test_build_teachpoint_joint_no_access() -> None:
    tp = build_teachpoint(
        coord_type="joint",
        position_id="wp1",
        coords=_JOINT_WIRE,
        access=None,
        orientation=None,
        gateway="gw1",
    )
    assert tp.is_joint_space()
    assert tp.gateway == "gw1"
    assert tp.access_config_name is None


def test_build_teachpoint_cartesian_without_orientation_raises() -> None:
    with pytest.raises(ValueError):
        build_teachpoint(
            coord_type="cartesian",
            position_id="slot1",
            coords=_CARTESIAN_WIRE,
            access=_access(),
            orientation=None,
            gateway=None,
        )
