"""Teachpoints on a transporter. Every route but delete needs a built runtime."""

from collections.abc import Mapping
from typing import ClassVar

from cheshire_drivers.move_parameters import MoveParameterPatch
from cheshire_drivers.teachpoints import Teachpoint
from pydantic import BaseModel, JsonValue

from orca.operations.binding import RestBinding
from orca.operations.catalog_models import NoContent
from orca.operations.device_commands import device_not_found
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError, message_of
from orca.operations.wire_models import TeachpointDTO
from orca.runtime.danger import record_audit
from orca.runtime.errors import NotATransporterError
from orca.runtime.facades.teachpoints import DELETE_TEACHPOINT
from orca.resource_models.move_parameters import MoveParameterEditRefused
from orca.runtime.facade_interfaces import IAccessConfigFacade, ITeachpointFacade
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.setup_files import SetupEntryExistsError
from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.runtime.teachpoint_wire import CoordValue, build_teachpoint, coord_type_for, validate_coords
from orca.operations.teachpoint_models import (
    CLEAR_LABWARE_OVERRIDE_ROUTE,
    DELETE_TEACHPOINT_ROUTE,
    GET_TEACHPOINT_ROUTE,
    LIST_TEACHPOINTS_ROUTE,
    SET_LABWARE_OVERRIDE_ROUTE,
    UPDATE_TEACHPOINT_ROUTE,
)
from orca.operations.teachpoint_models import (
    ClearLabwareOverrideRequest,
    DeleteTeachpointRequest,
    GetTeachpointRequest,
    ListTeachpointsRequest,
    SetLabwareOverrideRequest,
    TeachpointListResponse,
    UpdateTeachpointRequest,
)


def no_such_transporter(exc: KeyError, device_id: str) -> OperationError:
    """422 `validation_error` for a device that is not a transporter, else 404 `device_not_found`."""
    if isinstance(exc, NotATransporterError):
        return validation_error(message_of(exc), device_id)
    return device_not_found(device_id)


def validation_error(message: str, device_id: str | None = None, **extras: JsonValue) -> OperationError:
    """`validation_error`, for a teachpoint refused before it reaches storage."""
    if device_id is not None:
        extras["device_id"] = device_id
    return OperationError.of(ErrorCode.VALIDATION_ERROR, message, **extras)


def _not_found(message: str, device_id: str) -> OperationError:
    return OperationError.of(
        ErrorCode.TEACHPOINT_NOT_FOUND, message, device_id=device_id,
    )


def _no_such_teachpoint(device_id: str, name: str) -> OperationError:
    return _not_found(f"Teachpoint {name!r} not found for device {device_id!r}", device_id)


def already_exists(exc: SetupEntryExistsError, device_id: str) -> OperationError:
    """409 `already_exists` for a taken teachpoint name."""
    return OperationError.of(
        ErrorCode.ALREADY_EXISTS, str(exc), device_id=device_id,
    )


async def build_from_wire(
    access_configs: IAccessConfigFacade,
    *,
    position_id: str,
    coord_type: str,
    coords: Mapping[str, CoordValue],
    access_config_name: str | None,
    gateway: str | None,
    orientation: str | None,
    taught_with: str | None = None,
    by_labware: Mapping[str, MoveParameterPatch] | None = None,
) -> Teachpoint:
    """Look up the access config by name, then build. Raises KeyError or ValueError."""
    access = None
    if access_config_name is not None:
        access = await access_configs.get(access_config_name)
        if access is None:
            raise ValueError(f"AccessConfig {access_config_name!r} not found; create it first")
    return build_teachpoint(
        coord_type=coord_type,
        position_id=position_id,
        coords=coords,
        access=access,
        orientation=orientation,
        gateway=gateway,
        taught_with=taught_with,
        by_labware=by_labware,
    )


class ListTeachpointsOperation:
    Request: ClassVar[type[BaseModel]] = LIST_TEACHPOINTS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = LIST_TEACHPOINTS_ROUTE.response
    binding: ClassVar[RestBinding] = LIST_TEACHPOINTS_ROUTE.binding

    def __init__(self, *, runtime: ISystemRuntime) -> None:
        self._teachpoints: ITeachpointFacade = runtime.teachpoints

    async def run(self, req: ListTeachpointsRequest) -> TeachpointListResponse:
        if req.device_id is None:
            rows = await self._teachpoints.list_all()
            return TeachpointListResponse([TeachpointDTO.from_value(d, tp) for d, tp in rows])
        try:
            teachpoints = await self._teachpoints.list(req.device_id)
        except KeyError as exc:
            raise no_such_transporter(exc, req.device_id) from exc
        return TeachpointListResponse(
            [TeachpointDTO.from_value(req.device_id, tp) for tp in teachpoints],
        )


class GetTeachpointOperation:
    Request: ClassVar[type[BaseModel]] = GET_TEACHPOINT_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_TEACHPOINT_ROUTE.response
    binding: ClassVar[RestBinding] = GET_TEACHPOINT_ROUTE.binding

    def __init__(self, *, runtime: ISystemRuntime) -> None:
        self._teachpoints = runtime.teachpoints

    async def run(self, req: GetTeachpointRequest) -> TeachpointDTO:
        try:
            teachpoint = await self._teachpoints.get(req.device_id, req.name)
        except KeyError as exc:
            raise no_such_transporter(exc, req.device_id) from exc
        if teachpoint is None:
            raise _no_such_teachpoint(req.device_id, req.name)
        return TeachpointDTO.from_value(req.device_id, teachpoint)


class UpdateTeachpointOperation:
    """Replace the coords. The other fields change only when their update_ flag is set."""

    Request: ClassVar[type[BaseModel]] = UPDATE_TEACHPOINT_ROUTE.request
    Response: ClassVar[type[BaseModel]] = UPDATE_TEACHPOINT_ROUTE.response
    binding: ClassVar[RestBinding] = UPDATE_TEACHPOINT_ROUTE.binding

    def __init__(self, *, runtime: ISystemRuntime, access_configs: IAccessConfigFacade) -> None:
        self._teachpoints = runtime.teachpoints
        self._access_configs = access_configs

    async def run(self, req: UpdateTeachpointRequest) -> TeachpointDTO:
        try:
            existing = await self._teachpoints.get(req.device_id, req.name)
        except KeyError as exc:
            raise no_such_transporter(exc, req.device_id) from exc
        if existing is None:
            raise _no_such_teachpoint(req.device_id, req.name)
        coord_type = coord_type_for(existing)
        try:
            validate_coords(coord_type, req.coords)
        except ValueError as exc:
            raise validation_error(str(exc)) from exc
        try:
            teachpoint = await build_from_wire(
                self._access_configs,
                position_id=req.name,
                coord_type=coord_type,
                coords=req.coords,
                access_config_name=(
                    req.access_config_name if req.update_access_config else existing.access_config_name
                ),
                gateway=req.gateway if req.update_gateway else existing.gateway,
                orientation=req.orientation if req.update_orientation else existing.orientation,
                taught_with=req.taught_with if req.update_taught_with else existing.taught_with,
                by_labware=existing.by_labware,
            )
        except (KeyError, ValueError) as exc:
            raise validation_error(message_of(exc), req.device_id) from exc
        try:
            await self._teachpoints.update(req.device_id, teachpoint, reason=req.reason, confirm=True)
        except KeyError as exc:
            raise _no_such_teachpoint(req.device_id, req.name) from exc
        return TeachpointDTO.from_value(req.device_id, teachpoint)


class SetLabwareOverrideOperation:
    """Make one labware an exception at this one position."""

    Request: ClassVar[type[BaseModel]] = SET_LABWARE_OVERRIDE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = SET_LABWARE_OVERRIDE_ROUTE.response
    binding: ClassVar[RestBinding] = SET_LABWARE_OVERRIDE_ROUTE.binding

    def __init__(self, *, runtime: ISystemRuntime) -> None:
        self._teachpoints = runtime.teachpoints

    async def run(self, req: SetLabwareOverrideRequest) -> TeachpointDTO:
        try:
            teachpoint = await self._teachpoints.apply_labware_override(
                req.device_id, req.name, req.labware_type,
                req.set, req.clear, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise _not_found(message_of(exc), req.device_id) from exc
        except MoveParameterEditRefused as exc:
            raise OperationError.of(
                ErrorCode.VALIDATION_ERROR, str(exc), fields=list(exc.fields),
            ) from exc
        return TeachpointDTO.from_value(req.device_id, teachpoint)


class ClearLabwareOverrideOperation:
    """Hand this labware back to how the position handles everything else."""

    Request: ClassVar[type[BaseModel]] = CLEAR_LABWARE_OVERRIDE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = CLEAR_LABWARE_OVERRIDE_ROUTE.response
    binding: ClassVar[RestBinding] = CLEAR_LABWARE_OVERRIDE_ROUTE.binding

    def __init__(self, *, runtime: ISystemRuntime) -> None:
        self._teachpoints = runtime.teachpoints

    async def run(self, req: ClearLabwareOverrideRequest) -> NoContent:
        try:
            dropped = await self._teachpoints.clear_labware_override(
                req.device_id, req.name, req.labware_type, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise _not_found(message_of(exc), req.device_id) from exc
        if not dropped:
            raise _not_found(
                f"Teachpoint {req.name!r} on device {req.device_id!r} has no "
                f"override for labware {req.labware_type!r}",
                req.device_id,
            )
        return NoContent()


class DeleteTeachpointOperation:
    """Works with no built runtime, because a teachpoint under an unknown name breaks the build."""

    Request: ClassVar[type[BaseModel]] = DELETE_TEACHPOINT_ROUTE.request
    Response: ClassVar[type[BaseModel]] = DELETE_TEACHPOINT_ROUTE.response
    binding: ClassVar[RestBinding] = DELETE_TEACHPOINT_ROUTE.binding

    def __init__(
        self, *, runtime: ISystemRuntime | None, setup_stores: IRuntimeStoreFactory,
    ) -> None:
        self._runtime = runtime
        self._setup_stores = setup_stores

    async def run(self, req: DeleteTeachpointRequest) -> NoContent:
        if self._runtime is not None:
            try:
                deleted = await self._runtime.teachpoints.delete(
                    req.device_id, req.name, reason=req.reason, confirm=True,
                )
            except KeyError as exc:
                raise no_such_transporter(exc, req.device_id) from exc
        else:
            deleted = await self._delete_from_the_file(req)
        if not deleted:
            raise _no_such_teachpoint(req.device_id, req.name)
        return NoContent()

    async def _delete_from_the_file(self, req: DeleteTeachpointRequest) -> bool:
        deleted = await self._setup_stores.teachpoints(req.device_id).delete(req.name)
        if deleted:
            # The facade is not there to audit this delete.
            record_audit(
                DELETE_TEACHPOINT, req.reason,
                {"device_id": req.device_id, "position_id": req.name},
            )
        return deleted


TEACHPOINT_RUNTIME_OPERATIONS = (
    SetLabwareOverrideOperation,
    ClearLabwareOverrideOperation,
    ListTeachpointsOperation,
    GetTeachpointOperation,
)
