"""DB-neutral mapping between ``Teachpoint`` and ``TeachpointRow``.

Lives in orca-core; the per-DB teachpoint stores reuse it. The mapping is
dialect-independent and carries no engine knowledge. Access fields are
persisted inline so a teachpoint round-trips without an AccessConfig
lookup; ``access_config_name`` preserves the named-config FK so a re-read
reconstructs the same ``AccessConfig`` the topology declared.
"""

from typing import Literal, Optional

from cheshire_drivers.move_parameters import MoveParameterPatch
from cheshire_drivers.teachpoints import (
    AccessConfig,
    CartesianCoordinates,
    JointCoordinates,
    Teachpoint,
)
from pydantic import JsonValue

from orca.runtime.db.models import TeachpointRow


def _coords_to_row(
    coordinates: CartesianCoordinates | JointCoordinates | None,
) -> tuple[Optional[str], Optional[dict[str, JsonValue]]]:
    if isinstance(coordinates, CartesianCoordinates):
        return "cartesian", {
            "x": coordinates.x,
            "y": coordinates.y,
            "z": coordinates.z,
            "yaw": coordinates.yaw,
            "pitch": coordinates.pitch,
            "roll": coordinates.roll,
        }
    if isinstance(coordinates, JointCoordinates):
        return "joint", {
            "base": coordinates.base,
            "shoulder": coordinates.shoulder,
            "elbow": coordinates.elbow,
            "wrist": coordinates.wrist,
            "rail": coordinates.rail,
        }
    return None, None


def _coords_from_row(
    coord_type: Optional[str],
    coords: Optional[dict[str, JsonValue]],
) -> CartesianCoordinates | JointCoordinates | None:
    if coord_type is None or coords is None:
        return None
    if coord_type == "cartesian":
        return CartesianCoordinates(
            x=float(coords["x"]),
            y=float(coords["y"]),
            z=float(coords["z"]),
            yaw=float(coords["yaw"]),
            pitch=float(coords["pitch"]),
            roll=float(coords["roll"]),
        )
    if coord_type == "joint":
        return JointCoordinates(
            base=float(coords["base"]),
            shoulder=float(coords["shoulder"]),
            elbow=float(coords["elbow"]),
            wrist=float(coords["wrist"]),
            rail=float(coords.get("rail", 0.0)),
        )
    raise ValueError(f"Unknown coord_type {coord_type!r}")


def teachpoint_to_row(teachpoint: Teachpoint) -> TeachpointRow:
    coord_type, coords = _coords_to_row(teachpoint.coordinates)
    return TeachpointRow(
        position_id=teachpoint.position_id,
        coord_type=coord_type,
        coords=coords,
        orientation=teachpoint.orientation,
        gateway=teachpoint.gateway,
        access_config_name=teachpoint.access_config_name,
        access_type=teachpoint.access_type,
        gripper_offset=teachpoint.gripper_offset,
        vertical_clearance=teachpoint.vertical_clearance,
        horizontal_clearance=teachpoint.horizontal_clearance,
        taught_with=teachpoint.taught_with,
        by_labware=_by_labware_to_row(teachpoint),
    )


def apply_teachpoint_to_row(row: TeachpointRow, teachpoint: Teachpoint) -> None:
    """Overwrite a row's mutable fields from a teachpoint (update path; PK fixed)."""
    coord_type, coords = _coords_to_row(teachpoint.coordinates)
    row.coord_type = coord_type
    row.coords = coords
    row.orientation = teachpoint.orientation
    row.gateway = teachpoint.gateway
    row.access_config_name = teachpoint.access_config_name
    row.access_type = teachpoint.access_type
    row.gripper_offset = teachpoint.gripper_offset
    row.vertical_clearance = teachpoint.vertical_clearance
    row.horizontal_clearance = teachpoint.horizontal_clearance
    row.taught_with = teachpoint.taught_with
    row.by_labware = _by_labware_to_row(teachpoint)


def row_to_teachpoint(row: TeachpointRow) -> Teachpoint:
    coordinates = _coords_from_row(row.coord_type, row.coords)
    if row.access_config_name is not None and row.access_type is not None:
        access = AccessConfig(
            name=row.access_config_name,
            access_type=_validate_access_type(row.access_type),
            gripper_offset=_or_default(row.gripper_offset, 20.0),
            vertical_clearance=_or_default(row.vertical_clearance, 20.0),
            horizontal_clearance=_or_default(row.horizontal_clearance, 100.0),
        )
        return Teachpoint(
            position_id=row.position_id,
            coordinates=coordinates,
            orientation=row.orientation,
            access=access,
            gateway=row.gateway,
            taught_with=row.taught_with,
            by_labware=_by_labware_from_row(row),
        )
    return Teachpoint(
        position_id=row.position_id,
        coordinates=coordinates,
        orientation=row.orientation,
        access_type=row.access_type,
        gripper_offset=_or_default(row.gripper_offset, 20.0),
        vertical_clearance=_or_default(row.vertical_clearance, 20.0),
        horizontal_clearance=_or_default(row.horizontal_clearance, 100.0),
        gateway=row.gateway,
        taught_with=row.taught_with,
        by_labware=_by_labware_from_row(row),
    )


def _by_labware_to_row(teachpoint: Teachpoint) -> dict[str, JsonValue] | None:
    """Per-labware overrides as stored: sparse, and absent when there are none."""
    if not teachpoint.by_labware:
        return None
    return {
        labware_type: patch.model_dump(exclude_none=True)
        for labware_type, patch in teachpoint.by_labware.items()
    }


def _by_labware_from_row(row: TeachpointRow) -> dict[str, MoveParameterPatch]:
    return {
        labware_type: MoveParameterPatch.model_validate(patch)
        for labware_type, patch in (row.by_labware or {}).items()
    }


def _validate_access_type(value: str) -> Literal["vertical", "horizontal"]:
    if value not in ("vertical", "horizontal"):
        raise ValueError(f"Unknown access_type {value!r}")
    return value


def _or_default(value: Optional[float], default: float) -> float:
    return default if value is None else value
