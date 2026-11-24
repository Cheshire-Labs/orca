"""Read past runtime events by cursor."""

from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel

from orca.operations.binding import RestBinding
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.operations.route_tags import RouteTag
from orca.runtime.event_store import EventQuery, IEventStore
from orca.operations.binding import RestBinding, Route


class ListEventsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    since: int = Field(default=0, ge=0, description="Cursor to resume from.")
    execution_id: str | None = None
    limit: int = Field(default=500, ge=1, le=5000)
    tail: bool = Field(
        default=False,
        description=(
            "Return the newest `limit` events, oldest to newest, instead of replaying "
            "forward from `since`. Cannot be combined with `since`."
        ),
    )


class PastEvent(BaseModel):
    """A stored event in the shape the live stream sent it, and the cursor to resume after it."""

    model_config = ConfigDict(frozen=True)

    cursor: int
    event_name: str
    execution_id: str
    timestamp: float
    entity_type: str
    entity_id: str
    status: str
    context: dict[str, JsonValue]


class EventListResponse(RootModel[list[PastEvent]]):
    model_config = ConfigDict(frozen=True)


class IEventLog(Protocol):
    async def events(self, query: ListEventsRequest) -> list[PastEvent]: ...


class StoredEventLog(IEventLog):
    """Reads the events a server's `IEventStore` holds. A cursor is the row the store gave the event."""

    def __init__(self, store: IEventStore) -> None:
        self._store = store

    async def events(self, query: ListEventsRequest) -> list[PastEvent]:
        stored = await self._store.read(EventQuery(
            since=query.since, execution_id=query.execution_id, limit=query.limit, tail=query.tail,
        ))
        return [PastEvent.model_validate({**e.details, "cursor": e.cursor}) for e in stored]


LIST_EVENTS_ROUTE = Route(
    ListEventsRequest,
    EventListResponse,
    RestBinding(
        method="GET", path="/events", tag=RouteTag.EVENTS,
    ),
)


class ListEventsOperation:
    Request: ClassVar[type[BaseModel]] = LIST_EVENTS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = LIST_EVENTS_ROUTE.response
    binding: ClassVar[RestBinding] = LIST_EVENTS_ROUTE.binding

    def __init__(self, event_log: IEventLog) -> None:
        self._event_log = event_log

    async def run(self, req: ListEventsRequest) -> EventListResponse:
        if req.tail and req.since:
            raise OperationError(
                ErrorCode.VALIDATION_ERROR,
                "tail and since cannot be combined: tail takes the newest events, "
                "so paging with the cursor it returns would skip everything past "
                "limit. Drop tail to follow forward from a cursor.",
            )
        return EventListResponse(await self._event_log.events(req))
