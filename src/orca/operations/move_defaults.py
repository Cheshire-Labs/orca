"""Move defaults: what a transporter's moves start from. Served with or without a runtime."""

from typing import ClassVar

from pydantic import BaseModel

from orca.operations.binding import RestBinding
from orca.operations.catalog_models import EmptyRequest, NoContent
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.runtime.deployment_registries import IDeploymentRegistries
from orca.runtime.move_parameter_models import TransporterMoveDefaults
from orca.resource_models.move_parameters import MoveParameterEditRefused
from orca.system.system_interface import ISystem
from orca.operations.move_defaults_models import (
    GET_MOVE_DEFAULTS_ROUTE,
    LIST_MOVE_DEFAULTS_ROUTE,
    PATCH_MOVE_DEFAULTS_ROUTE,
    RESET_MOVE_DEFAULTS_ROUTE,
)
from orca.operations.move_defaults_models import (
    GetMoveDefaultsRequest,
    MoveDefaultsListResponse,
    PatchMoveDefaultsRequest,
    ResetMoveDefaultsRequest,
)


class ListMoveDefaultsOperation:
    """Every arm's starting numbers: the tuned ones, plus the mounted ones."""

    Request: ClassVar[type[BaseModel]] = LIST_MOVE_DEFAULTS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = LIST_MOVE_DEFAULTS_ROUTE.response
    binding: ClassVar[RestBinding] = LIST_MOVE_DEFAULTS_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries, system: ISystem | None) -> None:
        self._move_defaults = registries.move_defaults
        self._system = system

    async def run(self, req: EmptyRequest) -> MoveDefaultsListResponse:
        return MoveDefaultsListResponse(await self._move_defaults.list(self._system))


class GetMoveDefaultsOperation:
    """One arm's numbers and where each came from. An arm nobody tuned reads as the seed."""

    Request: ClassVar[type[BaseModel]] = GET_MOVE_DEFAULTS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_MOVE_DEFAULTS_ROUTE.response
    binding: ClassVar[RestBinding] = GET_MOVE_DEFAULTS_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._move_defaults = registries.move_defaults

    async def run(self, req: GetMoveDefaultsRequest) -> TransporterMoveDefaults:
        return await self._move_defaults.get(req.device_id)


class PatchMoveDefaultsOperation:
    """Change the fields named and leave the rest alone."""

    Request: ClassVar[type[BaseModel]] = PATCH_MOVE_DEFAULTS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = PATCH_MOVE_DEFAULTS_ROUTE.response
    binding: ClassVar[RestBinding] = PATCH_MOVE_DEFAULTS_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._move_defaults = registries.move_defaults

    async def run(self, req: PatchMoveDefaultsRequest) -> TransporterMoveDefaults:
        try:
            return await self._move_defaults.apply(
                req.device_id, req.set, req.clear, reason=req.reason, confirm=True,
            )
        except MoveParameterEditRefused as exc:
            raise OperationError.of(
                ErrorCode.VALIDATION_ERROR, str(exc), fields=list(exc.fields),
            ) from exc


class ResetMoveDefaultsOperation:
    """Discard everything tuned for this arm and put it back on the seed."""

    Request: ClassVar[type[BaseModel]] = RESET_MOVE_DEFAULTS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = RESET_MOVE_DEFAULTS_ROUTE.response
    binding: ClassVar[RestBinding] = RESET_MOVE_DEFAULTS_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._move_defaults = registries.move_defaults

    async def run(self, req: ResetMoveDefaultsRequest) -> NoContent:
        if not await self._move_defaults.reset(req.device_id, reason=req.reason, confirm=True):
            raise OperationError.of(
                ErrorCode.MOVE_DEFAULTS_NOT_FOUND,
                f"transporter {req.device_id!r} has no tuned move defaults",
            )
        return NoContent()


MOVE_DEFAULTS_OPERATIONS = (
    GetMoveDefaultsOperation,
    PatchMoveDefaultsOperation,
    ResetMoveDefaultsOperation,
)
