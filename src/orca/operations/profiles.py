"""Deployment profiles: named variable bundles. Editing one never touches a running execution."""

from typing import ClassVar

from pydantic import BaseModel

from orca.operations.binding import RestBinding
from orca.operations.catalog_models import EmptyRequest, NoContent
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError, message_of
from orca.runtime.deployment_registries import IDeploymentRegistries
from orca.variables.deployment_profile import DeploymentProfile
from orca.operations.profile_models import (
    CREATE_PROFILE_ROUTE,
    DELETE_PROFILE_ROUTE,
    GET_PROFILE_ROUTE,
    LIST_PROFILES_ROUTE,
    UPDATE_PROFILE_ROUTE,
)
from orca.operations.profile_models import (
    DeleteProfileRequest,
    ProfileListResponse,
    ProfileNameRequest,
    SaveProfileRequest,
)


def _not_found(name: str) -> OperationError:
    return OperationError.of(ErrorCode.PROFILE_NOT_FOUND, f"profile {name!r} not found")


class ListProfilesOperation:
    Request: ClassVar[type[BaseModel]] = LIST_PROFILES_ROUTE.request
    Response: ClassVar[type[BaseModel]] = LIST_PROFILES_ROUTE.response
    binding: ClassVar[RestBinding] = LIST_PROFILES_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._profiles = registries.profiles

    async def run(self, req: EmptyRequest) -> ProfileListResponse:
        return ProfileListResponse(await self._profiles.list())


class GetProfileOperation:
    Request: ClassVar[type[BaseModel]] = GET_PROFILE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_PROFILE_ROUTE.response
    binding: ClassVar[RestBinding] = GET_PROFILE_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._profiles = registries.profiles

    async def run(self, req: ProfileNameRequest) -> DeploymentProfile:
        profile = await self._profiles.get(req.name)
        if profile is None:
            raise _not_found(req.name)
        return profile


class CreateProfileOperation:
    Request: ClassVar[type[BaseModel]] = CREATE_PROFILE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = CREATE_PROFILE_ROUTE.response
    binding: ClassVar[RestBinding] = CREATE_PROFILE_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._profiles = registries.profiles

    async def run(self, req: SaveProfileRequest) -> DeploymentProfile:
        profile = req.profile()
        try:
            await self._profiles.add(profile, reason=req.reason, confirm=True)
        except ValueError as exc:
            raise OperationError.of(
                ErrorCode.ALREADY_EXISTS, str(exc),
            ) from exc
        return profile


class UpdateProfileOperation:
    Request: ClassVar[type[BaseModel]] = UPDATE_PROFILE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = UPDATE_PROFILE_ROUTE.response
    binding: ClassVar[RestBinding] = UPDATE_PROFILE_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._profiles = registries.profiles

    async def run(self, req: SaveProfileRequest) -> DeploymentProfile:
        profile = req.profile()
        try:
            await self._profiles.update(profile, reason=req.reason, confirm=True)
        except KeyError as exc:
            raise OperationError.of(ErrorCode.PROFILE_NOT_FOUND, message_of(exc)) from exc
        return profile


class DeleteProfileOperation:
    Request: ClassVar[type[BaseModel]] = DELETE_PROFILE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = DELETE_PROFILE_ROUTE.response
    binding: ClassVar[RestBinding] = DELETE_PROFILE_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._profiles = registries.profiles

    async def run(self, req: DeleteProfileRequest) -> NoContent:
        if not await self._profiles.delete(req.name, reason=req.reason, confirm=True):
            raise _not_found(req.name)
        return NoContent()


PROFILE_OPERATIONS = (
    ListProfilesOperation,
    GetProfileOperation,
    CreateProfileOperation,
    UpdateProfileOperation,
    DeleteProfileOperation,
)
