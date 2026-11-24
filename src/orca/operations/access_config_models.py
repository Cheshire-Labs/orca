"""Access config wire models and routes."""

from typing import Literal
from typing_extensions import Self

from cheshire_drivers.teachpoints import AccessConfig
from pydantic import BaseModel, ConfigDict, Field, RootModel
from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag
from orca.operations.catalog_models import EmptyRequest, NoContent


AccessType = Literal["vertical", "horizontal"]

DEFAULT_GRIPPER_OFFSET = 20.0
DEFAULT_VERTICAL_CLEARANCE = 20.0
DEFAULT_HORIZONTAL_CLEARANCE = 100.0


class CreateAccessConfigRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(description="Access config name, unique in the deployment")
    access_type: AccessType = Field(description="Access type: 'vertical' or 'horizontal'")
    gripper_offset: float = Field(default=DEFAULT_GRIPPER_OFFSET, description="Gripper offset in mm")
    vertical_clearance: float = Field(
        default=DEFAULT_VERTICAL_CLEARANCE, description="Vertical clearance in mm",
    )
    horizontal_clearance: float = Field(
        default=DEFAULT_HORIZONTAL_CLEARANCE, description="Horizontal clearance in mm",
    )
    reason: str | None = None


class GetAccessConfigRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str


class UpdateAccessConfigRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    access_type: AccessType | None = Field(
        default=None, description="Access type: 'vertical' or 'horizontal'",
    )
    gripper_offset: float | None = Field(default=None, description="Gripper offset in mm")
    vertical_clearance: float | None = Field(default=None, description="Vertical clearance in mm")
    horizontal_clearance: float | None = Field(
        default=None, description="Horizontal clearance in mm",
    )
    reason: str | None = None


class DeleteAccessConfigRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    reason: str | None = None


class AccessConfigResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    access_type: AccessType
    gripper_offset: float
    vertical_clearance: float
    horizontal_clearance: float

    @classmethod
    def from_value(cls, value: AccessConfig) -> Self:
        return cls(
            name=value.name,
            access_type=value.access_type,
            gripper_offset=value.gripper_offset,
            vertical_clearance=value.vertical_clearance,
            horizontal_clearance=value.horizontal_clearance,
        )


class AccessConfigListResponse(RootModel[list[AccessConfigResponse]]):
    model_config = ConfigDict(frozen=True)


CREATE_ACCESS_CONFIG_ROUTE = Route(
    CreateAccessConfigRequest,
    AccessConfigResponse,
    RestBinding(
        method="POST", path="/access-configs", tag=RouteTag.ACCESS_CONFIGS, status_code=201,
    ),
)


LIST_ACCESS_CONFIGS_ROUTE = Route(
    EmptyRequest,
    AccessConfigListResponse,
    RestBinding(
        method="GET", path="/access-configs", tag=RouteTag.ACCESS_CONFIGS,
    ),
)


GET_ACCESS_CONFIG_ROUTE = Route(
    GetAccessConfigRequest,
    AccessConfigResponse,
    RestBinding(
        method="GET", path="/access-configs/{name}", tag=RouteTag.ACCESS_CONFIGS,
    ),
)


UPDATE_ACCESS_CONFIG_ROUTE = Route(
    UpdateAccessConfigRequest,
    AccessConfigResponse,
    RestBinding(
        method="PUT", path="/access-configs/{name}", tag=RouteTag.ACCESS_CONFIGS,
    ),
)


DELETE_ACCESS_CONFIG_ROUTE = Route(
    DeleteAccessConfigRequest,
    NoContent,
    RestBinding(
        method="DELETE", path="/access-configs/{name}", tag=RouteTag.ACCESS_CONFIGS, status_code=204,
    ),
)
