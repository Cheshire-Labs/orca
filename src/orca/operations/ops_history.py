from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag
"""Ops history read Operation.

`SearchOpsHistoryOperation` filters the archive across every execution, or
within one. Naming an execution neither the live registry nor the durable
execution record knows answers ``execution_not_found`` instead of an empty list, while
an execution that finished before a restart still reads back. Records come
back in chronological order; ``limit`` keeps the newest.
"""

from orca.operations.route_tags import RouteTag
from orca.operations.binding import RestBinding
from typing import ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.state.records import (
    DeviceOperation,
    TrackingRecord,
    TrackingSource,
)
from orca.runtime.execution_record import ExecutionRecord
from orca.state.ops_store import OpsHistorySearchQuery


# -- Narrow capability surface ---------------------------------------------


class _OpsHistoryFacadeProtocol(Protocol):
    async def search(
        self, query: OpsHistorySearchQuery,
    ) -> list[tuple[str, TrackingRecord]]: ...


class _ExecutionRecordsProtocol(Protocol):
    async def contains(self, execution_id: str) -> bool: ...


@runtime_checkable
class _RuntimeWithOpsHistory(Protocol):
    """Narrow runtime surface for the ops history Operations.

    Both a hosted deployment's ``ISystemRuntime`` and the daemon's concrete
    ``SystemRuntime`` satisfy this structurally; the same friction
    ``_RuntimeWithSubmission`` handles for submissions. A search that names
    an execution needs the two existence probes: the live registry
    (``get_execution``) and the durable record (``execution_records``), so
    an execution that finished before a restart still reads back.
    """

    @property
    def ops_history(self) -> _OpsHistoryFacadeProtocol: ...

    @property
    def execution_records(self) -> _ExecutionRecordsProtocol: ...

    def get_execution(self, execution_id: str) -> ExecutionRecord: ...


# -- SearchOpsHistoryOperation ---------------------------------------------


class SearchOpsHistoryRequest(BaseModel):
    """Wire-side filters. AND-combine; empty body returns every record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: str | None = None
    action_id: str | None = None
    thread_id: str | None = None
    method_id: str | None = None
    source: str | None = None
    operation: str | None = None
    labware_name: str | None = None
    device_name: str | None = None
    limit: int | None = Field(default=None, ge=1, le=10000)

    def to_query(self) -> OpsHistorySearchQuery:
        return OpsHistorySearchQuery(
            execution_id=self.execution_id,
            action_id=self.action_id,
            thread_id=self.thread_id,
            method_id=self.method_id,
            source=(
                TrackingSource(self.source) if self.source is not None else None
            ),
            operation=(
                DeviceOperation(self.operation)
                if self.operation is not None else None
            ),
            labware_name=self.labware_name,
            device_name=self.device_name,
        )


class SearchOpsHistoryResponse(BaseModel):
    """Each record carries its owning ``execution_id``, in chronological order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    records: list[TrackingRecord] = Field(default_factory=list)


SEARCH_OPS_HISTORY_ROUTE = Route(
    SearchOpsHistoryRequest,
    SearchOpsHistoryResponse,
    RestBinding(
        method="GET", path="/ops-history", tag=RouteTag.OPS_HISTORY,
    ),
)


class SearchOpsHistoryOperation:
    Request: ClassVar[type[BaseModel]] = SEARCH_OPS_HISTORY_ROUTE.request
    Response: ClassVar[type[BaseModel]] = SEARCH_OPS_HISTORY_ROUTE.response
    binding: ClassVar[RestBinding] = SEARCH_OPS_HISTORY_ROUTE.binding

    def __init__(self, runtime: _RuntimeWithOpsHistory):
        self._runtime = runtime

    async def run(
        self, req: SearchOpsHistoryRequest,
    ) -> SearchOpsHistoryResponse:
        try:
            query = req.to_query()
        except ValueError as exc:
            raise OperationError.validation_error(
                f"invalid search query: {exc}",
            ) from exc
        if req.execution_id is not None:
            await self._refuse_an_unknown_execution(req.execution_id)
        records = sorted((rec for _eid, rec in await self._runtime.ops_history.search(query)), key=lambda r: r.timestamp)
        if req.limit is not None:
            records = records[-req.limit:]
        return SearchOpsHistoryResponse(records=records)

    async def _refuse_an_unknown_execution(self, execution_id: str) -> None:
        try:
            self._runtime.get_execution(execution_id)
        except KeyError as exc:
            # A restart empties the live registry but not the archive.
            if not await self._runtime.execution_records.contains(execution_id):
                raise OperationError.of(
                    ErrorCode.EXECUTION_NOT_FOUND,
                    f"execution {execution_id!r} not found",
                    execution_id=execution_id,
                ) from exc


OPS_HISTORY_OPERATIONS = (SearchOpsHistoryOperation,)
