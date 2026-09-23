"""list-ops-history answers from the durable record after a restart.

The gap this pins (state-ownership handoff appendix, finding 2): the operation
probed only the LIVE execution registry for existence, so after a restart a
finished execution's archived ops read back as ``not_found`` even though the
store held every record. Existence now falls back to the persisted
execution-record; the live registry stays the first (cheap) probe.
"""

import builtins
import time

import pytest

from orca.operations._protocol import OperationError
from orca.operations.ops_history import (
    ListOpsHistoryOperation,
    ListOpsHistoryRequest,
)
from orca.state.records import (
    DeviceOperation,
    GenericOperationDetails,
    OperationRecord,
    TrackingRecord,
    TrackingSource,
)
from orca.state.ops_store import OpsHistorySearchQuery


def _record(execution_id: str) -> TrackingRecord:
    now = time.time()
    return TrackingRecord(
        execution_id=execution_id,
        action_id="a1",
        thread_id="t1",
        method_id=None,
        source=TrackingSource.DECLARED,
        timestamp=now,
        operations=[
            OperationRecord(
                operation=DeviceOperation.SHAKE,
                device_name="shaker",
                affected_labware=["plate_1"],
                action_id="a1",
                thread_id="t1",
                details=GenericOperationDetails(command="shake", args_repr="()"),
                timestamp=now,
            )
        ],
    )


class _FakeOpsHistory:
    def __init__(self, records_by_execution: dict[str, list[TrackingRecord]]):
        self._records = records_by_execution

    # ``list`` shadows the builtin inside the class body, same as the facade.
    async def list(self, execution_id: str) -> builtins.list[TrackingRecord]:
        return builtins.list(self._records.get(execution_id, []))

    async def search(
        self, query: OpsHistorySearchQuery,
    ) -> builtins.list[tuple[str, TrackingRecord]]:
        raise NotImplementedError


class _FakeExecutionRecords:
    def __init__(self, known: set[str]):
        self._known = known

    async def contains(self, execution_id: str) -> bool:
        return execution_id in self._known


class _RestartedRuntime:
    """No live executions (the restart emptied the registry), but the durable
    record and the ops archive both know the execution."""

    def __init__(self, known: set[str], records: dict[str, list[TrackingRecord]]):
        self.ops_history = _FakeOpsHistory(records)
        self.execution_records = _FakeExecutionRecords(known)

    def get_execution(self, execution_id: str):
        raise KeyError(f"Execution '{execution_id}' not found")


async def test_archived_execution_reads_back_after_restart() -> None:
    runtime = _RestartedRuntime(
        known={"e1"}, records={"e1": [_record("e1")]},
    )
    op = ListOpsHistoryOperation(runtime)
    resp = await op.run(ListOpsHistoryRequest(execution_id="e1"))
    assert resp.execution_id == "e1"
    assert len(resp.records) == 1


async def test_unknown_everywhere_is_still_not_found() -> None:
    runtime = _RestartedRuntime(known=set(), records={})
    op = ListOpsHistoryOperation(runtime)
    with pytest.raises(OperationError) as exc_info:
        await op.run(ListOpsHistoryRequest(execution_id="ghost"))
    assert "not found" in str(exc_info.value)
