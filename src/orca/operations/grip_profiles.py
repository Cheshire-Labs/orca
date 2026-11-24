"""Grip profiles: how a labware type is held. Served with or without a runtime."""

from typing import ClassVar

from pydantic import BaseModel

from orca.operations.binding import RestBinding
from orca.operations.catalog_models import EmptyRequest, NoContent
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.runtime.deployment_registries import IDeploymentRegistries
from orca.runtime.move_parameter_models import LabwareGripProfile
from orca.resource_models.move_parameters import MoveParameterEditRefused
from orca.operations.grip_profile_models import (
    GET_GRIP_PROFILE_ROUTE,
    LIST_GRIP_PROFILES_ROUTE,
    PATCH_GRIP_PROFILE_ROUTE,
    RESET_GRIP_PROFILE_ROUTE,
)
from orca.operations.grip_profile_models import (
    GetGripProfileRequest,
    GripProfileListResponse,
    PatchGripProfileRequest,
    ResetGripProfileRequest,
)


class ListGripProfilesOperation:
    """Every labware type somebody has measured a grip for."""

    Request: ClassVar[type[BaseModel]] = LIST_GRIP_PROFILES_ROUTE.request
    Response: ClassVar[type[BaseModel]] = LIST_GRIP_PROFILES_ROUTE.response
    binding: ClassVar[RestBinding] = LIST_GRIP_PROFILES_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._grip_profiles = registries.grip_profiles

    async def run(self, req: EmptyRequest) -> GripProfileListResponse:
        return GripProfileListResponse(await self._grip_profiles.list())


class GetGripProfileOperation:
    """One type's grip. A type nobody measured reads as an empty profile."""

    Request: ClassVar[type[BaseModel]] = GET_GRIP_PROFILE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_GRIP_PROFILE_ROUTE.response
    binding: ClassVar[RestBinding] = GET_GRIP_PROFILE_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._grip_profiles = registries.grip_profiles

    async def run(self, req: GetGripProfileRequest) -> LabwareGripProfile:
        return await self._grip_profiles.get(req.labware_type)


class PatchGripProfileOperation:
    """Change the fields named and leave the rest inheriting."""

    Request: ClassVar[type[BaseModel]] = PATCH_GRIP_PROFILE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = PATCH_GRIP_PROFILE_ROUTE.response
    binding: ClassVar[RestBinding] = PATCH_GRIP_PROFILE_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._grip_profiles = registries.grip_profiles

    async def run(self, req: PatchGripProfileRequest) -> LabwareGripProfile:
        try:
            return await self._grip_profiles.apply(
                req.labware_type, req.set, req.clear, reason=req.reason, confirm=True,
            )
        except MoveParameterEditRefused as exc:
            raise OperationError.of(
                ErrorCode.VALIDATION_ERROR, str(exc), fields=list(exc.fields),
            ) from exc


class ResetGripProfileOperation:
    """Discard everything measured for this labware type."""

    Request: ClassVar[type[BaseModel]] = RESET_GRIP_PROFILE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = RESET_GRIP_PROFILE_ROUTE.response
    binding: ClassVar[RestBinding] = RESET_GRIP_PROFILE_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._grip_profiles = registries.grip_profiles

    async def run(self, req: ResetGripProfileRequest) -> NoContent:
        if not await self._grip_profiles.reset(req.labware_type, reason=req.reason, confirm=True):
            raise OperationError.of(ErrorCode.GRIP_PROFILE_NOT_FOUND, f"labware type {req.labware_type!r} has no grip profile")
        return NoContent()


GRIP_PROFILE_OPERATIONS = (
    ListGripProfilesOperation,
    GetGripProfileOperation,
    PatchGripProfileOperation,
    ResetGripProfileOperation,
)
