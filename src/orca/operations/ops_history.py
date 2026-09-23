"""Ops history read Operations.



`ListOpsHistoryOperation` returns every TrackingRecord archived for one
execution. `SearchOpsHistoryOperation` filters across all execution
buckets. Both route through ``runtime.ops_history`` so the in-memory
(orca-core) and Postgres-backed (a hosted deployment) stores share one Operation
surface.

The Pydantic Request mirrors the wire shape
binders deserialize into directly. The list Operation probes the live
execution registry, then the durable execution-record, so an unknown
execution_id surfaces as ``not_found`` instead of the silent empty list
the bare facade returns -- while an execution that finished before a
restart still reads back from the archive.
"""

import builtins
from typing import ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from orca.operations._protocol import OperationError
from orca.state.records import (
    DeviceOperation,
    TrackingRecord,
    TrackingSource,
)
from orca.runtime.execution_record import ExecutionRecord
from orca.state.ops_store import OpsHistorySearchQuery


# -- Narrow capability surface ---------------------------------------------


class _OpsHistoryFacadeProtocol(Protocol):
    # Method name shadows ``list`` inside its own annotation scope, so the
    # return type uses ``builtins.list`` to disambiguate (the runtime impl
    # is named ``OpsHistoryFacade.list`` and we cannot rename it here).
    async def list(  # noqa: A003 - matches facade method name
        self, execution_id: str,
    ) -> builtins.list[TrackingRecord]: ...

    async def search(
        self, query: OpsHistorySearchQuery,
    ) -> builtins.list[tuple[str, TrackingRecord]]: ...


class _ExecutionRecordsProtocol(Protocol):
    async def contains(self, execution_id: str) -> bool: ...


@runtime_checkable
class _RuntimeWithOpsHistory(Protocol):
    """Narrow runtime surface for the ops history Operations.

    Both a hosted deployment's ``ISystemRuntime`` and the daemon's concrete
    ``SystemRuntime`` satisfy this structurally; the same friction
    ``_RuntimeWithSubmission`` handles for submissions. ``ListOpsHistory``
    additionally needs the two existence probes: the live registry
    (``get_execution``) and the durable record (``execution_records``), so
    an execution that finished before a restart still reads back.
    """

    @property
    def ops_history(self) -> _OpsHistoryFacadeProtocol: ...

    @property
    def execution_records(self) -> _ExecutionRecordsProtocol: ...

    def get_execution(self, execution_id: str) -> ExecutionRecord: ...


# -- ListOpsHistoryOperation -----------------------------------------------


class ListOpsHistoryRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str
    limit: int | None = Field(
        default=None, ge=1, le=10000,
        description=(
            "Optional cap on the number of records returned. When set, "
            "the most recent `limit` records survive the cap (response "
            "preserves chronological order). The underlying store returns "
            "the full execution's record list before the cap is applied; "
            "this trims wire payload, not memory or DB scan cost."
        ),
    )


class ListOpsHistoryResponse(BaseModel):
    """``execution_id`` mirrors the request for caller verification.

    Every entry in ``records`` also carries the same ``execution_id``
    at the record level (canonical ``TrackingRecord`` has it as a
    required field) so the inner shape matches the search endpoint.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: str
    records: list[TrackingRecord]


class ListOpsHistoryOperation:
    Request: ClassVar[type[BaseModel]] = ListOpsHistoryRequest
    Response: ClassVar[type[BaseModel]] = ListOpsHistoryResponse

    def __init__(self, runtime: _RuntimeWithOpsHistory):
        self._runtime = runtime

    async def run(
        self, req: ListOpsHistoryRequest,
    ) -> ListOpsHistoryResponse:
        try:
            self._runtime.get_execution(req.execution_id)
        except KeyError as exc:
            # A restart empties the live registry but not the archive: fall
            # back to the durable record before declaring the id unknown.
            if not await self._runtime.execution_records.contains(req.execution_id):
                raise OperationError.not_found(
                    f"execution {req.execution_id!r} not found",
                    execution_id=req.execution_id,
                ) from exc

        records = await self._runtime.ops_history.list(req.execution_id)

        if req.limit is not None and len(records) > req.limit:
            tail = sorted(records, key=lambda r: r.timestamp, reverse=True)
            tail = tail[: req.limit]
            records = sorted(tail, key=lambda r: r.timestamp)

        return ListOpsHistoryResponse(
            execution_id=req.execution_id,
            records=list(records),
        )


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
    """Cross-execution result. ``records`` matches the per-execution
    Operation's ``records`` field; each entry carries its owning
    ``execution_id`` via the canonical ``TrackingRecord``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    records: list[TrackingRecord] = Field(default_factory=list)


class SearchOpsHistoryOperation:
    Request: ClassVar[type[BaseModel]] = SearchOpsHistoryRequest
    Response: ClassVar[type[BaseModel]] = SearchOpsHistoryResponse

    def __init__(self, runtime: _RuntimeWithOpsHistory):
        self._runtime = runtime

    async def run(
        self, req: SearchOpsHistoryRequest,
    ) -> SearchOpsHistoryResponse:
        try:
            query = req.to_query()
        except ValueError as exc:
            raise OperationError.invalid_input(
                f"invalid search query: {exc}",
            ) from exc
        hits = await self._runtime.ops_history.search(query)
        if req.limit is not None and len(hits) > req.limit:
            hits = sorted(
                hits, key=lambda h: h[1].timestamp, reverse=True,
            )[: req.limit]
        return SearchOpsHistoryResponse(
            records=[rec for _eid, rec in hits],
        )
