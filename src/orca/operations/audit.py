"""Read the audit trail of confirmed dangerous actions."""

from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field, RootModel

from orca.operations.wire_models import AuditEntryDTO
from orca.operations.binding import RestBinding
from orca.operations.route_tags import RouteTag
from orca.runtime.audit_store import AuditQuery, IAuditStore
from orca.operations.binding import RestBinding, Route


class ListAuditRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    action_name: str | None = None
    danger_level: str | None = None
    since: float | None = None
    until: float | None = None
    limit: int = Field(default=200, ge=1, le=2000)


class AuditListResponse(RootModel[list[AuditEntryDTO]]):
    model_config = ConfigDict(frozen=True)


class IAuditLog(Protocol):
    async def newest_first(self, query: ListAuditRequest) -> list[AuditEntryDTO]: ...


class StoredAuditLog(IAuditLog):
    """Reads the audit rows a server's `IAuditStore` holds."""

    def __init__(self, store: IAuditStore) -> None:
        self._store = store

    async def newest_first(self, query: ListAuditRequest) -> list[AuditEntryDTO]:
        entries = await self._store.newest_first(AuditQuery(
            action_name=query.action_name,
            danger_level=query.danger_level,
            since=query.since,
            until=query.until,
            limit=query.limit,
        ))
        return [
            AuditEntryDTO(
                timestamp=e.timestamp,
                action_name=e.action_name,
                danger_level=e.danger_level,
                reason=e.reason,
                call_args=dict(e.call_args),
            )
            for e in entries
        ]


LIST_AUDIT_ROUTE = Route(
    ListAuditRequest,
    AuditListResponse,
    RestBinding(
        method="GET", path="/audit", tag=RouteTag.AUDIT,
    ),
)


class ListAuditOperation:
    Request: ClassVar[type[BaseModel]] = LIST_AUDIT_ROUTE.request
    Response: ClassVar[type[BaseModel]] = LIST_AUDIT_ROUTE.response
    binding: ClassVar[RestBinding] = LIST_AUDIT_ROUTE.binding

    def __init__(self, audit_log: IAuditLog) -> None:
        self._audit_log = audit_log

    async def run(self, req: ListAuditRequest) -> AuditListResponse:
        return AuditListResponse(await self._audit_log.newest_first(req))
