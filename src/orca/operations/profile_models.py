"""Deployment profile wire models and routes."""

from pydantic import BaseModel, ConfigDict, RootModel

from orca.variables.deployment_profile import DeploymentProfile
from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag
from orca.operations.catalog_models import EmptyRequest, NoContent


class ProfileNameRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str


class DeleteProfileRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    reason: str | None = None


class SaveProfileRequest(DeploymentProfile):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str | None = None

    def profile(self) -> DeploymentProfile:
        return DeploymentProfile.model_validate(self.model_dump(exclude={"reason"}))


class ProfileListResponse(RootModel[list[DeploymentProfile]]):
    model_config = ConfigDict(frozen=True)


LIST_PROFILES_ROUTE = Route(
    EmptyRequest,
    ProfileListResponse,
    RestBinding(method="GET", path="/profiles", tag=RouteTag.PROFILES),
)


GET_PROFILE_ROUTE = Route(
    ProfileNameRequest,
    DeploymentProfile,
    RestBinding(
        method="GET", path="/profiles/{name}", tag=RouteTag.PROFILES,
    ),
)


CREATE_PROFILE_ROUTE = Route(
    SaveProfileRequest,
    DeploymentProfile,
    RestBinding(
        method="POST", path="/profiles", tag=RouteTag.PROFILES, status_code=201,
    ),
)


UPDATE_PROFILE_ROUTE = Route(
    SaveProfileRequest,
    DeploymentProfile,
    RestBinding(
        method="PUT", path="/profiles/{name}", tag=RouteTag.PROFILES,
    ),
)


DELETE_PROFILE_ROUTE = Route(
    DeleteProfileRequest,
    NoContent,
    RestBinding(
        method="DELETE", path="/profiles/{name}", tag=RouteTag.PROFILES, status_code=204,
    ),
)
