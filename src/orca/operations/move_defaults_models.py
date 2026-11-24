"""Move defaults wire models and routes."""

from pydantic import BaseModel, ConfigDict, RootModel

from orca.runtime.move_parameter_models import MoveDefaultsPatchRequest, TransporterMoveDefaults
from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag
from orca.operations.catalog_models import EmptyRequest, NoContent


class GetMoveDefaultsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str


class PatchMoveDefaultsRequest(MoveDefaultsPatchRequest):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str


class ResetMoveDefaultsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str
    reason: str | None = None


class MoveDefaultsListResponse(RootModel[list[TransporterMoveDefaults]]):
    model_config = ConfigDict(frozen=True)


LIST_MOVE_DEFAULTS_ROUTE = Route(
    EmptyRequest,
    MoveDefaultsListResponse,
    RestBinding(
        method="GET", path="/move-defaults", tag=RouteTag.MOVE_DEFAULTS,
    ),
)


GET_MOVE_DEFAULTS_ROUTE = Route(
    GetMoveDefaultsRequest,
    TransporterMoveDefaults,
    RestBinding(
        method="GET", path="/move-defaults/{device_id}", tag=RouteTag.MOVE_DEFAULTS,
    ),
)


PATCH_MOVE_DEFAULTS_ROUTE = Route(
    PatchMoveDefaultsRequest,
    TransporterMoveDefaults,
    RestBinding(
        method="PATCH", path="/move-defaults/{device_id}", tag=RouteTag.MOVE_DEFAULTS,
    ),
)


RESET_MOVE_DEFAULTS_ROUTE = Route(
    ResetMoveDefaultsRequest,
    NoContent,
    RestBinding(
        method="DELETE", path="/move-defaults/{device_id}", tag=RouteTag.MOVE_DEFAULTS,
        status_code=204,
    ),
)
