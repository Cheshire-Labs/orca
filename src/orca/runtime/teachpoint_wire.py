"""Wire-format teachpoint conversion + validation shared by both backends.

The teachpoint write surface speaks in flat coord dicts plus a top-level
``coord_type`` string, while stores and drivers exchange typed
``cheshire_drivers.teachpoints`` value objects. This module is the single
copy of that boundary logic so the orca daemon and the hosted REST surface
convert and validate identically.

The wire envelope carries ``coord_type`` and ``orientation`` at the top
level; the nested coords dict must not duplicate them.
"""

from collections.abc import Mapping
from typing import Union

from cheshire_drivers.move_parameters import MoveParameterPatch
from cheshire_drivers.teachpoints import (
    AccessConfig,
    CartesianCoordinates,
    JointCoordinates,
    Teachpoint,
)

CoordValue = Union[str, int, float]

COORD_TYPE_CARTESIAN = "cartesian"
COORD_TYPE_JOINT = "joint"
VALID_COORD_TYPES = {COORD_TYPE_CARTESIAN, COORD_TYPE_JOINT}

CARTESIAN_REQUIRED_FIELDS = ["x", "y", "z"]
JOINT_REQUIRED_FIELDS = ["rail", "base", "shoulder", "elbow", "wrist", "gripper"]

_CARTESIAN_ALLOWED: frozenset[str] = frozenset(
    {"type", "x", "y", "z", "yaw", "pitch", "roll", "orientation"}
)
_JOINT_ALLOWED: frozenset[str] = frozenset(
    {"type", "rail", "base", "shoulder", "elbow", "wrist", "gripper", "orientation"}
)


class InvalidTeachpointCoordsError(ValueError):
    """Coord validation failure. A ValueError so daemon routes map it to 400."""


def reject_top_level_dupes_in_coords(coords: Mapping[str, CoordValue]) -> None:
    """Reject `type` and `orientation` inside the inner `coords` dict.

    The wire envelope promotes `coord_type` and `orientation` to top-level
    fields; permitting them inside `coords` allows two sources of truth on
    the same payload.
    """
    if "type" in coords:
        raise InvalidTeachpointCoordsError(
            "'type' must not appear inside 'coords'; "
            "use the top-level 'coord_type' field instead",
        )
    if "orientation" in coords:
        raise InvalidTeachpointCoordsError(
            "'orientation' must not appear inside 'coords'; "
            "use the top-level 'orientation' field instead",
        )


def validate_cartesian_coords(coords: Mapping[str, CoordValue]) -> None:
    """Validate cartesian coordinate structure (extra-forbid).

    Unknown field names raise so a typo (e.g. ``rx/ry/rz`` instead of
    ``yaw/pitch/roll``) cannot silently default the teachpoint's rotation
    to zero.
    """
    required_fields = ["type", "x", "y", "z"]
    for field in required_fields:
        if field not in coords:
            raise InvalidTeachpointCoordsError(f"Missing required field: {field}")

    if coords["type"] != "cartesian":
        raise InvalidTeachpointCoordsError(
            f"Coordinate type must be 'cartesian', got '{coords['type']}'"
        )

    unknown = sorted(set(coords) - _CARTESIAN_ALLOWED)
    if unknown:
        raise InvalidTeachpointCoordsError(
            f"Unknown cartesian coord field(s): {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(_CARTESIAN_ALLOWED))}"
        )

    position_fields = ["x", "y", "z"]
    for field in position_fields:
        if not isinstance(coords[field], (int, float)):
            raise InvalidTeachpointCoordsError(f"Field '{field}' must be a number")

    rotation_fields = ["yaw", "pitch", "roll"]
    for field in rotation_fields:
        if field in coords and not isinstance(coords[field], (int, float)):
            raise InvalidTeachpointCoordsError(f"Field '{field}' must be a number")

    if "orientation" in coords:
        if coords["orientation"] not in ["left", "right"]:
            raise InvalidTeachpointCoordsError(
                f"Orientation must be 'left' or 'right', got '{coords['orientation']}'"
            )


def validate_joint_coords(coords: Mapping[str, CoordValue]) -> None:
    """Validate joint coordinate structure (extra-forbid).

    Unknown field names raise so a typo cannot silently default a joint
    axis to zero.
    """
    required_fields = ["type", "rail", "base", "shoulder", "elbow", "wrist", "gripper"]
    for field in required_fields:
        if field not in coords:
            raise InvalidTeachpointCoordsError(f"Missing required field: {field}")

    if coords["type"] != "joint":
        raise InvalidTeachpointCoordsError(
            f"Coordinate type must be 'joint', got '{coords['type']}'"
        )

    unknown = sorted(set(coords) - _JOINT_ALLOWED)
    if unknown:
        raise InvalidTeachpointCoordsError(
            f"Unknown joint coord field(s): {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(_JOINT_ALLOWED))}"
        )

    joint_fields = ["rail", "base", "shoulder", "elbow", "wrist", "gripper"]
    for field in joint_fields:
        if not isinstance(coords[field], (int, float)):
            raise InvalidTeachpointCoordsError(f"Field '{field}' must be a number")


def validate_coords(coord_type: str, coords: Mapping[str, CoordValue]) -> None:
    """Validate a wire-shape coords dict against the flat envelope.

    The envelope carries `coord_type` and `orientation` at the top level;
    the nested shape (`coords.type`, `coords.orientation`) is rejected here
    so two-source-of-truth bugs cannot land.
    """
    if coord_type not in VALID_COORD_TYPES:
        raise InvalidTeachpointCoordsError(
            f"Invalid coord_type: {coord_type}. Must be 'cartesian' or 'joint'",
        )
    reject_top_level_dupes_in_coords(coords)
    merged: dict[str, CoordValue] = {**dict(coords), "type": coord_type}
    if coord_type == COORD_TYPE_CARTESIAN:
        validate_cartesian_coords(merged)
    else:
        validate_joint_coords(merged)


def coords_to_typed(
    coord_type: str, coords: Mapping[str, CoordValue],
) -> CartesianCoordinates | JointCoordinates:
    """Convert a wire-format coords dict to a typed Coordinates value."""
    if coord_type == COORD_TYPE_CARTESIAN:
        return CartesianCoordinates(
            x=float(coords["x"]),
            y=float(coords["y"]),
            z=float(coords["z"]),
            yaw=float(coords.get("yaw", 0.0)),
            pitch=float(coords.get("pitch", 0.0)),
            roll=float(coords.get("roll", 0.0)),
        )
    return JointCoordinates(
        rail=float(coords.get("rail", 0.0)),
        base=float(coords["base"]),
        shoulder=float(coords["shoulder"]),
        elbow=float(coords["elbow"]),
        wrist=float(coords["wrist"]),
        gripper=float(coords.get("gripper", 0.0)),
    )


def typed_to_wire(tp: Teachpoint) -> dict[str, CoordValue]:
    """Convert a typed Teachpoint's coordinates to the wire dict shape.

    Top-level `coord_type` is the single source of truth; the inner coords
    dict does not echo a duplicate `type` field.
    """
    if tp.coordinates is None:
        return {}
    payload: dict[str, CoordValue] = {}
    payload.update(tp.coordinates.to_dict())
    return payload


def coord_type_for(tp: Teachpoint) -> str:
    if tp.coordinates is None:
        return ""
    return tp.coordinates.coord_type


def build_teachpoint(
    coord_type: str,
    position_id: str,
    coords: Mapping[str, CoordValue],
    access: AccessConfig | None,
    orientation: str | None,
    gateway: str | None,
    taught_with: str | None = None,
    by_labware: Mapping[str, MoveParameterPatch | Mapping[str, float | str]] | None = None,
) -> Teachpoint:
    """Construct a Teachpoint from wire fields and an already-resolved access.

    The caller resolves an ``access_config_name`` to an ``AccessConfig``
    value (or None) and passes it in; this module stays decoupled from the
    access-config facade.

    ``taught_with`` records the labware this position was jogged to, and
    ``by_labware`` carries per-labware overrides for this one position. Both
    are optional: a position that names neither behaves exactly as it did
    before either existed.
    """
    coordinates = coords_to_typed(coord_type, coords)
    return Teachpoint(
        position_id=position_id,
        coordinates=coordinates,
        orientation=orientation,
        access=access,
        gateway=gateway,
        taught_with=taught_with,
        by_labware={
            labware_type: MoveParameterPatch.model_validate(patch)
            for labware_type, patch in (by_labware or {}).items()
        },
    )
