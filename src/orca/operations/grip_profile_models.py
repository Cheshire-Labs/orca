"""Grip profile wire models and routes."""

from pydantic import BaseModel, ConfigDict, RootModel

from orca.runtime.move_parameter_models import GripProfilePatchRequest, LabwareGripProfile
from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag
from orca.operations.catalog_models import EmptyRequest, NoContent


class GetGripProfileRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    labware_type: str


class PatchGripProfileRequest(GripProfilePatchRequest):
    model_config = ConfigDict(frozen=True, extra="forbid")

    labware_type: str


class ResetGripProfileRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    labware_type: str
    reason: str | None = None


class GripProfileListResponse(RootModel[list[LabwareGripProfile]]):
    model_config = ConfigDict(frozen=True)


LIST_GRIP_PROFILES_ROUTE = Route(
    EmptyRequest,
    GripProfileListResponse,
    RestBinding(
        method="GET", path="/grip-profiles", tag=RouteTag.GRIP_PROFILES,
    ),
)


GET_GRIP_PROFILE_ROUTE = Route(
    GetGripProfileRequest,
    LabwareGripProfile,
    RestBinding(
        method="GET", path="/grip-profiles/{labware_type}", tag=RouteTag.GRIP_PROFILES,
    ),
)


PATCH_GRIP_PROFILE_ROUTE = Route(
    PatchGripProfileRequest,
    LabwareGripProfile,
    RestBinding(
        method="PATCH", path="/grip-profiles/{labware_type}", tag=RouteTag.GRIP_PROFILES,
    ),
)


RESET_GRIP_PROFILE_ROUTE = Route(
    ResetGripProfileRequest,
    NoContent,
    RestBinding(
        method="DELETE", path="/grip-profiles/{labware_type}", tag=RouteTag.GRIP_PROFILES,
        status_code=204,
    ),
)
