"""Create teachpoints on a transporter: one row or many, all saved or none."""

from collections.abc import Mapping, Sequence
from typing import ClassVar, Literal

from cheshire_drivers.move_parameters import MoveParameterPatch
from cheshire_drivers.teachpoints import Teachpoint
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from typing_extensions import Self

from orca.operations.binding import RestBinding
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError, message_of
from orca.operations.teachpoint_transfer_models import (
    CreateTeachpointsRequest,
    CreateTeachpointsResponse,
)
from orca.operations.teachpoints import build_from_wire, no_such_transporter, validation_error
from orca.runtime.deployment_registries import IDeploymentRegistries
from orca.runtime.facade_interfaces import IAccessConfigFacade, ITeachpointFacade
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.setup_files import SetupEntryExistsError
from orca.runtime.teachpoint_wire import validate_coords
from orca.operations.teachpoint_transfer_models import CREATE_TEACHPOINTS_ROUTE


class TeachpointInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str | None = Field(
        default=None,
        description=(
            "Accepted so an export row imports as it is. Ignored: the import "
            "request's device_id says where the teachpoints go."
        ),
    )
    position_id: str = Field(...)
    coord_type: Literal["cartesian", "joint"] = Field(...)
    coords: dict[str, str | int | float] = Field(...)
    access_config_name: str | None = Field(default=None)
    gateway: str | None = Field(default=None)
    orientation: Literal["left", "right"] | None = Field(default=None)
    taught_with: str | None = Field(default=None)
    by_labware: dict[str, MoveParameterPatch] | None = Field(default=None)

    @model_validator(mode="after")
    def _orientation_matches_coord_type(self) -> Self:
        if self.coord_type == "cartesian" and self.orientation is None:
            raise ValueError("Cartesian teachpoints require orientation ('left' or 'right')")
        return self


class RowRefused(ValueError):
    """A bulk-import row that cannot be saved. The message names the row."""


_ROW = TypeAdapter(TeachpointInput)


def _row_label(index: int, raw: Mapping[str, JsonValue]) -> str:
    position_id = raw.get("position_id")
    if isinstance(position_id, str) and position_id:
        return f"Teachpoint {position_id!r}"
    return f"Teachpoint at index {index}"


def _format_errors(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error["loc"])
        parts.append(f"{loc}: {error['msg']}" if loc else error["msg"])
    return "; ".join(parts)


def validate_row(index: int, raw: Mapping[str, JsonValue]) -> TeachpointInput:
    label = _row_label(index, raw)
    try:
        row = _ROW.validate_python(raw)
    except ValidationError as exc:
        raise RowRefused(f"{label}: {_format_errors(exc)}") from exc
    try:
        validate_coords(row.coord_type, row.coords)
    except ValueError as exc:
        raise RowRefused(f"{label}: {exc}") from exc
    return row


async def _build(
    access_configs: IAccessConfigFacade, label: str, row: TeachpointInput,
) -> Teachpoint:
    try:
        return await build_from_wire(
            access_configs,
            position_id=row.position_id,
            coord_type=row.coord_type,
            coords=row.coords,
            access_config_name=row.access_config_name,
            gateway=row.gateway,
            orientation=row.orientation,
            taught_with=row.taught_with,
            by_labware=row.by_labware,
        )
    except (KeyError, ValueError) as exc:
        raise RowRefused(f"{label}: {message_of(exc)}") from exc


class RowsRefused(ValueError):
    """At least one row cannot be saved, so none is. Holds one line per refused row."""

    def __init__(self, errors: list[str], total: int) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} of {total} teachpoints refused; nothing was saved")


class NamesTaken(ValueError):
    """Rows name teachpoints the device already has, and `overwrite` is off. Nothing is saved."""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        super().__init__(
            f"teachpoints already exist: {', '.join(repr(n) for n in names)}. Nothing was saved; "
            "send overwrite=true to replace them."
        )


async def _undo(
    teachpoints: ITeachpointFacade, device_id: str, saved: Sequence[Teachpoint], before: Mapping[str, Teachpoint],
) -> None:
    """Put back what a refused request had already saved, so a create saves every row or none."""
    undo = "undoing a teachpoint create the store refused part-way"
    for teachpoint in reversed(saved):
        previous = before.get(teachpoint.position_id)
        if previous is None:
            await teachpoints.delete(device_id, teachpoint.position_id, reason=undo, confirm=True)
        else:
            await teachpoints.update(device_id, previous, reason=undo, confirm=True)


async def create_teachpoints(
    teachpoints: ITeachpointFacade,
    access_configs: IAccessConfigFacade,
    device_id: str,
    rows: Sequence[dict[str, JsonValue]],
    overwrite: bool,
    *,
    reason: str | None = None,
) -> CreateTeachpointsResponse:
    """Save every row or none. Raises KeyError for an unknown device or one that is not a transporter."""
    before = {tp.position_id: tp for tp in await teachpoints.list(device_id)}
    existing = set(before)
    built: list[Teachpoint] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows):
        label = _row_label(index, raw)
        try:
            row = validate_row(index, raw)
            teachpoint = await _build(access_configs, label, row)
        except RowRefused as exc:
            errors.append(str(exc))
            continue
        if teachpoint.position_id in seen:
            errors.append(f"{label} is named twice in this request")
            continue
        seen.add(teachpoint.position_id)
        built.append(teachpoint)
    if errors:
        raise RowsRefused(errors, len(rows))
    taken = [tp.position_id for tp in built if tp.position_id in existing]
    if taken and not overwrite:
        raise NamesTaken(taken)
    saved: list[Teachpoint] = []
    try:
        for teachpoint in built:
            if teachpoint.position_id in existing:
                await teachpoints.update(device_id, teachpoint, reason=reason, confirm=True)
            else:
                await teachpoints.add(device_id, teachpoint, reason=reason, confirm=True)
            saved.append(teachpoint)
    except (KeyError, ValueError, SetupEntryExistsError):
        await _undo(teachpoints, device_id, saved, before)
        raise
    updated = len(taken)
    created = len(built) - updated
    return CreateTeachpointsResponse(
        total=len(rows),
        created=created,
        updated=updated,
        summary=f"saved {len(built)} teachpoints ({created} created, {updated} updated)",
    )


class CreateTeachpointsOperation:
    """Save teachpoints on a transporter: every row or none. Overwrite replaces an existing name."""

    Request: ClassVar[type[BaseModel]] = CREATE_TEACHPOINTS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = CREATE_TEACHPOINTS_ROUTE.response
    binding: ClassVar[RestBinding] = CREATE_TEACHPOINTS_ROUTE.binding

    def __init__(
        self, *, runtime: ISystemRuntime, registries: IDeploymentRegistries,
    ) -> None:
        self._teachpoints = runtime.teachpoints
        self._access_configs = registries.access_configs

    async def run(self, req: CreateTeachpointsRequest) -> CreateTeachpointsResponse:
        try:
            return await create_teachpoints(
                self._teachpoints,
                self._access_configs,
                req.device_id,
                req.teachpoints,
                req.overwrite,
                reason=req.reason,
            )
        except KeyError as exc:
            raise no_such_transporter(exc, req.device_id) from exc
        except RowsRefused as exc:
            raise validation_error(str(exc), req.device_id, errors=list(exc.errors)) from exc
        except NamesTaken as exc:
            raise OperationError.of(
                ErrorCode.ALREADY_EXISTS, str(exc), device_id=req.device_id, names=list(exc.names),
            ) from exc
        except SetupEntryExistsError as exc:
            raise OperationError.of(ErrorCode.CONFLICT, message_of(exc), device_id=req.device_id) from exc
        except ValueError as exc:
            raise validation_error(message_of(exc), req.device_id) from exc
