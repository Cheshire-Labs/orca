"""System info wire models and routes."""

from pydantic import BaseModel, ConfigDict
from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag


class GetSystemInfoRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GetSystemInfoResponse(BaseModel):
    """The ``is_simulating`` field is gone because
    run-mode is request-scope (per-submission), not deployment-
    scope. Effective mode per device is exposed via the device-status
    Operations + ``SubmissionDTO.run_mode``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    description: str
    version: str
    device_count: int
    transporter_count: int
    workflow_count: int
    method_count: int


GET_SYSTEM_INFO_ROUTE = Route(
    GetSystemInfoRequest,
    GetSystemInfoResponse,
    RestBinding(
        method="GET", path="/system", tag=RouteTag.SYSTEM,
    ),
)
