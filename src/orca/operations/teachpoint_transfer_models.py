"""Wire models for creating teachpoints. The CLI imports these without the engine."""

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag


class CreateTeachpointsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str
    teachpoints: list[dict[str, JsonValue]] = Field(
        description=(
            "Teachpoint rows: position_id, coord_type, coords, orientation, "
            "access_config_name, gateway, taught_with, by_labware, and device_id, "
            "which is accepted and ignored so a row read from GET /api/teachpoints "
            "saves as it is. Every row is checked before any is saved: one refused "
            "row refuses the request with 422 `validation_error`, every refused row "
            "listed in `extras.errors`. A name the device already has answers 409 "
            "`already_exists` unless `overwrite` is true."
        ),
    )
    overwrite: bool = False
    reason: str | None = None


class CreateTeachpointsResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int
    created: int
    updated: int
    summary: str


CREATE_TEACHPOINTS_ROUTE = Route(
    CreateTeachpointsRequest,
    CreateTeachpointsResponse,
    RestBinding(
        method="POST", path="/teachpoints/{device_id}", tag=RouteTag.TEACHPOINTS, status_code=201,
    ),
)
