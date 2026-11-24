"""search-ops-history for one execution answers from the durable record after a restart.

The live execution registry is the first existence probe; a restart empties it,
so a finished execution's archived ops must still read back from the persisted
execution record. An execution neither knows is ``execution_not_found``, not an empty list.
"""

import pytest

from orca.operations.protocol import OperationError
from orca.operations.ops_history import (
    SearchOpsHistoryOperation,
    SearchOpsHistoryRequest,
)
from orca.runtime.execution_record import ExecutionRecord
from orca.state.records import (
    DeviceOperation,
    GenericOperationDetails,
    OperationRecord,
    TrackingRecord,
    TrackingSource,
)
from orca.state.ops_store import OpsHistorySearchQuery


def _record(execution_id: str, action_id: str = "a1", timestamp: float = 100.0) -> TrackingRecord:
    return TrackingRecord(
        execution_id=execution_id,
        action_id=action_id,
        thread_id="t1",
        method_id=None,
        source=TrackingSource.DECLARED,
        timestamp=timestamp,
        operations=[
            OperationRecord(
                operation=DeviceOperation.SHAKE,
                device_name="shaker",
                affected_labware=["plate_1"],
                action_id=action_id,
                thread_id="t1",
                details=GenericOperationDetails(command="shake", args_repr="()"),
                timestamp=timestamp,
            )
        ],
    )


class _FakeOpsHistory:
    def __init__(self, records: list[TrackingRecord]):
        self._records = records

    async def search(self, query: OpsHistorySearchQuery) -> list[tuple[str, TrackingRecord]]:
        return [
            (r.execution_id, r) for r in self._records
            if query.execution_id is None or r.execution_id == query.execution_id
        ]


class _FakeExecutionRecords:
    def __init__(self, known: set[str]):
        self._known = known

    async def contains(self, execution_id: str) -> bool:
        return execution_id in self._known


class _RestartedRuntime:
    """No live executions (the restart emptied the registry), but the durable
    record and the ops archive both know the execution."""

    def __init__(self, known: set[str], records: list[TrackingRecord]):
        self.ops_history = _FakeOpsHistory(records)
        self.execution_records = _FakeExecutionRecords(known)

    def get_execution(self, execution_id: str) -> ExecutionRecord:
        raise KeyError(f"Execution '{execution_id}' not found")


async def test_archived_execution_reads_back_after_restart() -> None:
    op = SearchOpsHistoryOperation(_RestartedRuntime(known={"e1"}, records=[_record("e1")]))
    resp = await op.run(SearchOpsHistoryRequest(execution_id="e1"))
    assert [r.execution_id for r in resp.records] == ["e1"]


async def test_an_execution_unknown_everywhere_is_not_found() -> None:
    op = SearchOpsHistoryOperation(_RestartedRuntime(known=set(), records=[]))
    with pytest.raises(OperationError) as exc_info:
        await op.run(SearchOpsHistoryRequest(execution_id="ghost"))
    assert "not found" in str(exc_info.value)


async def test_a_search_naming_no_execution_never_checks_one() -> None:
    op = SearchOpsHistoryOperation(_RestartedRuntime(known=set(), records=[_record("e1")]))
    resp = await op.run(SearchOpsHistoryRequest())
    assert len(resp.records) == 1


async def test_records_come_back_oldest_first_and_a_limit_keeps_the_newest() -> None:
    records = [_record("e1", "late", 300.0), _record("e1", "early", 100.0), _record("e1", "middle", 200.0)]
    op = SearchOpsHistoryOperation(_RestartedRuntime(known={"e1"}, records=records))

    everything = await op.run(SearchOpsHistoryRequest(execution_id="e1"))
    newest_two = await op.run(SearchOpsHistoryRequest(execution_id="e1", limit=2))

    assert [r.action_id for r in everything.records] == ["early", "middle", "late"]
    assert [r.action_id for r in newest_two.records] == ["middle", "late"]
