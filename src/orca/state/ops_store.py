"""OpsHistory archive registry: store + view + search-query model.

Per-execution archive of TrackingRecords. Keyed by execution_id; system-level
records (initial-state seeds before any execution exists, runtime startup
events) live under the sentinel ``SYSTEM_ID``.

Single source of truth: every TrackingRecord append goes through the store;
``OpsHistory`` is a thin per-execution writer that calls
``store.append(execution_id, record)``. Reads happen through the facade
(``runtime.ops_history.list(execution_id)`` / ``search(query)``) or directly
via the store on the System (back-compat for ledger projections).

Cross-execution search is supported through OpsHistorySearchQuery (Pydantic).
The source-available default appends JSONL files; a hosted deployment injects its own
indexed store behind the same Protocol.
"""

from typing import List, Optional, Protocol, Tuple

from pydantic import BaseModel, ConfigDict

from orca.state.records import (
    DeviceOperation,
    OperationRecord,
    TrackingRecord,
    TrackingSource,
)


SYSTEM_ID = "_system"
"""Universal sentinel for system-emitted record identifiers.

Used in both ``execution_id`` and ``thread_id`` positions on records that
predate any user execution (initial-state labware seeds, runtime startup
events). One value, one constant: any code that needs to stand in for
"there is no real id here, this is the system" uses this. There is never
a second sentinel; if a future caller wants a different placeholder, they
either pass a real id or accept ``None`` at a nullable boundary.

Per-execution lookups exclude this bucket by default; it is reachable via
explicit ``list('_system')`` / ``for_execution``.
"""


class OpsHistorySearchQuery(BaseModel):
    """Field-equality filters across all execution buckets.

    Empty query (all fields None) returns every record paired with its
    bucket's execution_id. Multiple non-None fields are AND-combined. This
    model is the wire shape REST/MCP send.
    """

    model_config = ConfigDict(extra="forbid")

    execution_id: Optional[str] = None
    action_id: Optional[str] = None
    thread_id: Optional[str] = None
    method_id: Optional[str] = None
    source: Optional[TrackingSource] = None
    operation: Optional[DeviceOperation] = None
    labware_name: Optional[str] = None
    device_name: Optional[str] = None


class IOpsHistoryView(Protocol):
    """Per-execution view of the OpsHistory archive.

    Cheap handle scoped to one execution_id. ``list`` returns the bucketed
    records in append order. The view does not cache; reads round-trip to
    the underlying store.
    """

    @property
    def execution_id(self) -> str: ...

    async def list(self) -> List[TrackingRecord]: ...

    async def all_operations(self) -> List[OperationRecord]: ...

    async def ops_for(self, labware_name: str) -> List[OperationRecord]: ...


class IOpsHistoryStore(Protocol):
    """Per-execution archive of TrackingRecords.

    Records are appended keyed by execution_id; reads are scoped to one
    execution at a time. ``search`` is the cross-execution query: it returns
    matches ordered by ascending ``record.timestamp`` across all execution
    buckets (ties retain a stable, implementation-defined order). Every impl
    must honor that ordering so consumers get one chronological merge no
    matter which backing store answered. The source-available default appends JSONL files;
    a hosted deployment injects its own indexed archive that survives runtime
    restarts.
    """

    async def append(self, execution_id: str, record: TrackingRecord) -> None: ...

    def for_execution(self, execution_id: str) -> IOpsHistoryView: ...

    async def list(self, execution_id: str) -> List[TrackingRecord]: ...

    async def search(
        self, query: OpsHistorySearchQuery,
    ) -> List[Tuple[str, TrackingRecord]]: ...


async def ops_for_labware(
    store: IOpsHistoryStore, labware_name: str, labware_id: str,
) -> List[OperationRecord]:
    """Chronological ops touching one labware INSTANCE, across every bucket.

    The instance-mediated read (``LabwareInstance.ops()``) sees only the
    buckets something bound, and a restart severs those bindings; this is the
    store-truth read that survives it.

    Scoped by instance id, not just name: PLR-backed residents reuse a fixed
    name every boot, and retire keeps ops, so a name-only fold would hand a
    fresh successor the dead namesake's volumes. An op that carries
    ``affected_labware_ids`` must name this instance; an op with no ids
    (driver-reported name only) folds by name, since it cannot be attributed.
    """
    hits = await store.search(OpsHistorySearchQuery(labware_name=labware_name))
    ops = [
        op
        for _execution_id, record in hits
        for op in record.operations
        if labware_name in op.affected_labware
        and (not op.affected_labware_ids or labware_id in op.affected_labware_ids)
    ]
    ops.sort(key=lambda op: (op.timestamp, op.sequence))
    return ops


def record_matches(record: TrackingRecord, query: OpsHistorySearchQuery) -> bool:
    """Field-equality match reused by every store impl's search.

    Filters on top-level record fields are direct equality. Operation-level
    filters (operation, labware_name, device_name) match when ANY of the
    record's operations satisfies the constraint.
    """
    if query.action_id is not None and record.action_id != query.action_id:
        return False
    if query.thread_id is not None and record.thread_id != query.thread_id:
        return False
    if query.method_id is not None and record.method_id != query.method_id:
        return False
    if query.source is not None and record.source != query.source:
        return False
    if query.operation is not None:
        if not any(op.operation == query.operation for op in record.operations):
            return False
    if query.labware_name is not None:
        if not any(query.labware_name in op.affected_labware for op in record.operations):
            return False
    if query.device_name is not None:
        if not any(op.device_name == query.device_name for op in record.operations):
            return False
    return True


class StoreBackedOpsHistoryView:
    """Per-execution view that delegates to any IOpsHistoryStore.

    Holds nothing but the store and an execution_id; every read round-trips to
    ``store.list``. Shared by all store impls so the delegating boilerplate
    lives once.
    """

    def __init__(self, store: IOpsHistoryStore, execution_id: str) -> None:
        self._store = store
        self._execution_id = execution_id

    @property
    def execution_id(self) -> str:
        return self._execution_id

    async def list(self) -> List[TrackingRecord]:
        return await self._store.list(self._execution_id)

    async def all_operations(self) -> List[OperationRecord]:
        records = await self._store.list(self._execution_id)
        return [op for rec in records for op in rec.operations]

    async def ops_for(self, labware_name: str) -> List[OperationRecord]:
        ops = await self.all_operations()
        return [op for op in ops if labware_name in op.affected_labware]


async def ops_for_device(
    store: IOpsHistoryStore, device_name: str,
) -> List[OperationRecord]:
    """Chronological ops this device performed, across every bucket.

    A run writes into its own execution bucket, so the system bucket alone
    answers "nothing has ever happened" on every real deployment.
    """
    hits = await store.search(OpsHistorySearchQuery(device_name=device_name))
    ops = [
        op
        for _execution_id, record in hits
        for op in record.operations
        if op.device_name == device_name
    ]
    ops.sort(key=lambda op: (op.timestamp, op.sequence))
    return ops
