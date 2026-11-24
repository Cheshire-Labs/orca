"""Resolve a transporter's taught position, and the gateway waypoints in front of it, to the wire shape a command sends."""

from cheshire_drivers.teachpoints import Teachpoint
from cheshire_drivers.transporter_models import TeachpointModel
from pydantic import JsonValue

from orca.resource_models.device_error import TeachpointNotFoundError
from orca.runtime.facade_interfaces import ITeachpointFacade
from orca.runtime.teachpoint_wire import CARTESIAN_REQUIRED_FIELDS, JOINT_REQUIRED_FIELDS


class InvalidPositionError(ValueError):
    """Position data a command cannot be sent with."""


class TeachpointResolver:
    """Resolve position ids to the teachpoint dicts a transporter command carries."""

    def __init__(self, teachpoints: ITeachpointFacade) -> None:
        self._teachpoints = teachpoints

    async def resolve(self, device_id: str, position: str | dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Resolve a position id, or pass a coordinate dict through once it carries every axis."""
        if isinstance(position, dict):
            _require_axes(position)
            return position
        teachpoint = await self._fetch(device_id, position)
        # The receiver forbids extra fields, so the wire shape is the model's own dump.
        wire: dict[str, JsonValue] = TeachpointModel.from_driver_teachpoint(teachpoint).model_dump(
            mode="json", exclude_none=True,
        )
        return wire

    async def resolve_gateway_chain(self, device_id: str, position_id: str) -> list[dict[str, JsonValue]]:
        """The gateway waypoints in front of a position, innermost first."""
        teachpoint = await self._fetch(device_id, position_id)
        chain: list[dict[str, JsonValue]] = []
        visited: set[str] = set()
        current = teachpoint.gateway
        while current:
            if current in visited:
                raise InvalidPositionError(f"Circular gateway reference detected: {current}")
            visited.add(current)
            chain.append(await self.resolve(device_id, current))
            hop = await self._teachpoints.get(device_id, current)
            current = hop.gateway if hop is not None else None
        return chain

    async def _fetch(self, device_id: str, position_id: str) -> Teachpoint:
        teachpoint = await self._teachpoints.get(device_id, position_id)
        if teachpoint is None:
            available = [tp.position_id for tp in await self._teachpoints.list(device_id)]
            raise TeachpointNotFoundError(
                f"No teachpoint registered for position_id {position_id!r} on device {device_id!r}",
                available_teachpoints=available,
            )
        return teachpoint


def _require_axes(coords: dict[str, JsonValue]) -> None:
    coord_type = coords.get("coord_type")
    if coord_type not in ("cartesian", "joint"):
        raise InvalidPositionError(f"coord_type must be 'cartesian' or 'joint', got {coord_type!r}")
    required = CARTESIAN_REQUIRED_FIELDS if coord_type == "cartesian" else JOINT_REQUIRED_FIELDS
    for field in required:
        if field not in coords:
            raise InvalidPositionError(f"Missing required field {field!r} for {coord_type} coordinates")


def validate_pick_place_teachpoint(teachpoint: dict[str, JsonValue]) -> None:
    """Refuse a joint teachpoint for a pick or place: the approach and retreat are cartesian."""
    coord_type = teachpoint.get("coord_type")
    if coord_type != "cartesian":
        position_id = teachpoint.get("position_id", "unknown")
        raise InvalidPositionError(
            f"Teachpoint '{position_id}' uses {coord_type} coordinates. "
            "Pick/place operations require cartesian coordinates for inverse kinematics. "
            "Use transporter.move_to for joint coordinate waypoints."
        )
