"""Thread, reservation and plugin read models and routes."""

from pydantic import BaseModel, ConfigDict, Field, RootModel

from orca.operations.wire_models import (
    PluginDTO,
    ReservationSnapshotDTO,
    ThreadSnapshotResponse,
)
from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag
from orca.operations.wire_models import ReservationCancelResponse


class _Empty(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GetThreadRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str
    thread_id: str


class ReservationListResponse(RootModel[list[ReservationSnapshotDTO]]):
    model_config = ConfigDict(frozen=True)


class ListReservationsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str | None = Field(
        default=None, description="Only this run's reservations; 404 `execution_not_found` when there is no such run.",
    )


class CancelReservationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str
    reservation_id: str
    reason: str


class ListPluginsRequest(_Empty):
    pass


class PluginListResponse(RootModel[list[PluginDTO]]):
    model_config = ConfigDict(frozen=True)




GET_THREAD_ROUTE = Route(
    GetThreadRequest,
    ThreadSnapshotResponse,
    RestBinding(
        method="GET", path="/executions/{execution_id}/threads/{thread_id}",
        tag=RouteTag.THREADS,
    ),
)


LIST_RESERVATIONS_ROUTE = Route(
    ListReservationsRequest,
    ReservationListResponse,
    RestBinding(
        method="GET", path="/reservations", tag=RouteTag.RESERVATIONS,
    ),
)


CANCEL_RESERVATION_ROUTE = Route(
    CancelReservationRequest,
    ReservationCancelResponse,
    RestBinding(
        method="DELETE", path="/executions/{execution_id}/reservations/{reservation_id}",
        tag=RouteTag.RESERVATIONS, carries_blockers=True,
    ),
)


LIST_PLUGINS_ROUTE = Route(
    ListPluginsRequest,
    PluginListResponse,
    RestBinding(
        method="GET", path="/plugins", tag=RouteTag.PLUGINS,
    ),
)

