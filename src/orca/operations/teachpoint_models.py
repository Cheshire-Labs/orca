"""Teachpoint wire models and routes."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

from orca.operations.wire_models import TeachpointDTO
from orca.runtime.move_parameter_models import GripProfilePatchRequest
from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag
from orca.operations.catalog_models import NoContent
from orca.runtime.teachpoint_wire import CoordValue


class ListTeachpointsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str | None = Field(
        default=None,
        description=(
            "Device identifier. When omitted, returns every teachpoint across every "
            "transporter on the running system; each row carries its own `device_id`."
        ),
    )


class GetTeachpointRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str
    name: str


class UpdateTeachpointRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str
    name: str
    coords: dict[str, CoordValue] = Field(description="New coordinate data")
    access_config_name: str | None = None
    gateway: str | None = None
    orientation: Literal["left", "right"] | None = None
    taught_with: str | None = None
    update_access_config: bool = False
    update_gateway: bool = False
    update_orientation: bool = False
    update_taught_with: bool = False
    reason: str | None = None


class SetLabwareOverrideRequest(GripProfilePatchRequest):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str
    name: str
    labware_type: str


class ClearLabwareOverrideRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str
    name: str
    labware_type: str
    reason: str | None = None


class DeleteTeachpointRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str
    name: str
    reason: str | None = None


class TeachpointListResponse(RootModel[list[TeachpointDTO]]):
    model_config = ConfigDict(frozen=True)


LIST_TEACHPOINTS_ROUTE = Route(
    ListTeachpointsRequest,
    TeachpointListResponse,
    RestBinding(
        method="GET", path="/teachpoints", tag=RouteTag.TEACHPOINTS,
    ),
)


GET_TEACHPOINT_ROUTE = Route(
    GetTeachpointRequest,
    TeachpointDTO,
    RestBinding(
        method="GET", path="/teachpoints/{device_id}/{name:path}", tag=RouteTag.TEACHPOINTS,
    ),
)


UPDATE_TEACHPOINT_ROUTE = Route(
    UpdateTeachpointRequest,
    TeachpointDTO,
    RestBinding(
        method="PUT", path="/teachpoints/{device_id}/{name:path}", tag=RouteTag.TEACHPOINTS,
    ),
)


SET_LABWARE_OVERRIDE_ROUTE = Route(
    SetLabwareOverrideRequest,
    TeachpointDTO,
    RestBinding(
        method="PATCH",
        path="/teachpoint-overrides/{device_id}/{labware_type}/{name:path}",
        tag=RouteTag.TEACHPOINTS,
    ),
)


CLEAR_LABWARE_OVERRIDE_ROUTE = Route(
    ClearLabwareOverrideRequest,
    NoContent,
    RestBinding(
        method="DELETE",
        path="/teachpoint-overrides/{device_id}/{labware_type}/{name:path}",
        tag=RouteTag.TEACHPOINTS,
        status_code=204,
    ),
)


DELETE_TEACHPOINT_ROUTE = Route(
    DeleteTeachpointRequest,
    NoContent,
    RestBinding(
        method="DELETE", path="/teachpoints/{device_id}/{name:path}", tag=RouteTag.TEACHPOINTS,
        status_code=204,
    ),
)
