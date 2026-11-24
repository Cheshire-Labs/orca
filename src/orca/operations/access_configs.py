"""Access configs: how an arm approaches a position. Served with or without a runtime."""

from typing import ClassVar

from cheshire_drivers.teachpoints import AccessConfig
from pydantic import BaseModel

from orca.operations.binding import RestBinding
from orca.operations.catalog_models import EmptyRequest, NoContent
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.runtime.access_config_store import AccessConfigInUseError
from orca.runtime.deployment_registries import IDeploymentRegistries
from orca.operations.access_config_models import (
    CREATE_ACCESS_CONFIG_ROUTE,
    DELETE_ACCESS_CONFIG_ROUTE,
    GET_ACCESS_CONFIG_ROUTE,
    LIST_ACCESS_CONFIGS_ROUTE,
    UPDATE_ACCESS_CONFIG_ROUTE,
)
from orca.operations.access_config_models import (
    AccessConfigListResponse,
    AccessConfigResponse,
    CreateAccessConfigRequest,
    DeleteAccessConfigRequest,
    GetAccessConfigRequest,
    UpdateAccessConfigRequest,
)

def _not_found(name: str) -> OperationError:
    return OperationError.of(
        ErrorCode.ACCESS_CONFIG_NOT_FOUND, f"Access config {name!r} not found",
    )


class CreateAccessConfigOperation:
    Request: ClassVar[type[BaseModel]] = CREATE_ACCESS_CONFIG_ROUTE.request
    Response: ClassVar[type[BaseModel]] = CREATE_ACCESS_CONFIG_ROUTE.response
    binding: ClassVar[RestBinding] = CREATE_ACCESS_CONFIG_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._access_configs = registries.access_configs

    async def run(self, req: CreateAccessConfigRequest) -> AccessConfigResponse:
        config = AccessConfig(
            name=req.name,
            access_type=req.access_type,
            gripper_offset=req.gripper_offset,
            vertical_clearance=req.vertical_clearance,
            horizontal_clearance=req.horizontal_clearance,
        )
        try:
            await self._access_configs.add(config, reason=req.reason, confirm=True)
        except ValueError as exc:
            raise OperationError.of(
                ErrorCode.ALREADY_EXISTS, str(exc), name=config.name,
            ) from exc
        return AccessConfigResponse.from_value(config)


class ListAccessConfigsOperation:
    Request: ClassVar[type[BaseModel]] = LIST_ACCESS_CONFIGS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = LIST_ACCESS_CONFIGS_ROUTE.response
    binding: ClassVar[RestBinding] = LIST_ACCESS_CONFIGS_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._access_configs = registries.access_configs

    async def run(self, req: EmptyRequest) -> AccessConfigListResponse:
        configs = await self._access_configs.list()
        return AccessConfigListResponse([AccessConfigResponse.from_value(c) for c in configs])


class GetAccessConfigOperation:
    Request: ClassVar[type[BaseModel]] = GET_ACCESS_CONFIG_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_ACCESS_CONFIG_ROUTE.response
    binding: ClassVar[RestBinding] = GET_ACCESS_CONFIG_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._access_configs = registries.access_configs

    async def run(self, req: GetAccessConfigRequest) -> AccessConfigResponse:
        config = await self._access_configs.get(req.name)
        if config is None:
            raise _not_found(req.name)
        return AccessConfigResponse.from_value(config)


class UpdateAccessConfigOperation:
    """Overwrite the named fields. Unnamed fields keep their stored value."""

    Request: ClassVar[type[BaseModel]] = UPDATE_ACCESS_CONFIG_ROUTE.request
    Response: ClassVar[type[BaseModel]] = UPDATE_ACCESS_CONFIG_ROUTE.response
    binding: ClassVar[RestBinding] = UPDATE_ACCESS_CONFIG_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._access_configs = registries.access_configs

    async def run(self, req: UpdateAccessConfigRequest) -> AccessConfigResponse:
        fields = (req.access_type, req.gripper_offset, req.vertical_clearance, req.horizontal_clearance)
        if all(value is None for value in fields):
            raise OperationError.of(
                ErrorCode.VALIDATION_ERROR,
                "At least one field must be provided for update",
            )
        existing = await self._access_configs.get(req.name)
        if existing is None:
            raise _not_found(req.name)
        merged = AccessConfig(
            name=req.name,
            access_type=req.access_type or existing.access_type,
            gripper_offset=(
                req.gripper_offset if req.gripper_offset is not None else existing.gripper_offset
            ),
            vertical_clearance=(
                req.vertical_clearance
                if req.vertical_clearance is not None
                else existing.vertical_clearance
            ),
            horizontal_clearance=(
                req.horizontal_clearance
                if req.horizontal_clearance is not None
                else existing.horizontal_clearance
            ),
        )
        try:
            await self._access_configs.update(merged, reason=req.reason, confirm=True)
        except KeyError as exc:
            raise _not_found(req.name) from exc
        return AccessConfigResponse.from_value(merged)


class DeleteAccessConfigOperation:
    Request: ClassVar[type[BaseModel]] = DELETE_ACCESS_CONFIG_ROUTE.request
    Response: ClassVar[type[BaseModel]] = DELETE_ACCESS_CONFIG_ROUTE.response
    binding: ClassVar[RestBinding] = DELETE_ACCESS_CONFIG_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._access_configs = registries.access_configs

    async def run(self, req: DeleteAccessConfigRequest) -> NoContent:
        try:
            deleted = await self._access_configs.delete(req.name, reason=req.reason, confirm=True)
        except AccessConfigInUseError as exc:
            raise OperationError.of(
                ErrorCode.RESOURCE_IN_USE, str(exc),
                referencing_count=exc.referencing_count,
            ) from exc
        if not deleted:
            raise _not_found(req.name)
        return NoContent()


ACCESS_CONFIG_OPERATIONS = (
    CreateAccessConfigOperation,
    ListAccessConfigsOperation,
    GetAccessConfigOperation,
    UpdateAccessConfigOperation,
    DeleteAccessConfigOperation,
)
