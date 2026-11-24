"""SqliteOpsHistoryStore: records bucketed by execution, listed in write order, searched in time order."""

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from orca.runtime.sqlite_ops_history_store import SqliteOpsHistoryStore
from orca.state.ops_store import OpsHistorySearchQuery
from orca.state.records import (
    AspirateDetails,
    DeviceOperation,
    DispenseDetails,
    OperationDetails,
    OperationRecord,
    TrackingRecord,
    TrackingSource,
)
from tests.ephemeral_ops_store import ops_history_engine


def _record(
    action_id: str = "a1",
    ops: list[OperationRecord] | None = None,
    execution_id: str = "exec-test",
    timestamp: float | None = None,
) -> TrackingRecord:
    return TrackingRecord(
        execution_id=execution_id,
        action_id=action_id,
        thread_id="t1",
        method_id="m1",
        source=TrackingSource.OBSERVED,
        timestamp=time.time() if timestamp is None else timestamp,
        operations=ops or [],
    )


def _op(op_type: DeviceOperation, labware: str, details: OperationDetails, device: str = "lh") -> OperationRecord:
    return OperationRecord(
        operation=op_type,
        device_name=device,
        affected_labware=[labware],
        action_id="a1",
        thread_id="t1",
        details=details,
        timestamp=time.time(),
    )


def _aspirate(labware: str, device: str = "lh") -> OperationRecord:
    return _op(
        DeviceOperation.ASPIRATE, labware,
        AspirateDetails(labware=labware, positions=["A1"], volumes=[10.0]), device,
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def store(db_path: Path) -> SqliteOpsHistoryStore:
    return SqliteOpsHistoryStore(ops_history_engine(db_path))


async def test_list_returns_one_executions_records_in_write_order(store: SqliteOpsHistoryStore) -> None:
    first, second, other = _record("a1", timestamp=200.0), _record("a2", timestamp=100.0), _record("b1")
    await store.append("exec-1", first)
    await store.append("exec-1", second)
    await store.append("exec-2", other)
    assert await store.list("exec-1") == [first, second]
    assert await store.for_execution("exec-2").list() == [other]
    assert await store.list("never-existed") == []


async def test_a_reopened_store_reads_what_an_earlier_one_wrote(db_path: Path) -> None:
    record = _record("durable")
    await SqliteOpsHistoryStore(ops_history_engine(db_path)).append("exec-1", record)
    reopened = SqliteOpsHistoryStore(ops_history_engine(db_path))
    assert await reopened.list("exec-1") == [record]
    assert await reopened.search(OpsHistorySearchQuery()) == [("exec-1", record)]


async def test_search_is_chronological_across_executions(store: SqliteOpsHistoryStore) -> None:
    late = _record("late", execution_id="a-exec", timestamp=200.0)
    early = _record("early", execution_id="z-exec", timestamp=100.0)
    await store.append("a-exec", late)
    await store.append("z-exec", early)
    assert [r.action_id for _, r in await store.search(OpsHistorySearchQuery())] == ["early", "late"]


async def test_search_filters_on_record_fields(store: SqliteOpsHistoryStore) -> None:
    needle, hay = _record("needle"), _record("hay")
    await store.append("exec-1", needle)
    await store.append("exec-2", hay)
    assert await store.search(OpsHistorySearchQuery(action_id="needle")) == [("exec-1", needle)]
    assert await store.search(OpsHistorySearchQuery(execution_id="exec-2")) == [("exec-2", hay)]


async def test_search_by_labware_returns_only_records_that_touch_it(store: SqliteOpsHistoryStore) -> None:
    plate1 = _record("a1", ops=[_aspirate("plate1")])
    plate2 = _record("a2", ops=[_aspirate("plate2")])
    await store.append("exec-1", plate1)
    await store.append("exec-2", plate2)
    assert await store.search(OpsHistorySearchQuery(labware_name="plate1")) == [("exec-1", plate1)]


async def test_search_by_operation_and_device_matches_any_operation_in_a_record(
    store: SqliteOpsHistoryStore,
) -> None:
    aspirate = _record("a1", ops=[_aspirate("plate1", device="lh_1")])
    dispense = _record("a2", ops=[_op(
        DeviceOperation.DISPENSE, "plate1",
        DispenseDetails(labware="plate1", positions=["A1"], volumes=[10.0]), "lh_2",
    )])
    await store.append("exec-1", aspirate)
    await store.append("exec-1", dispense)
    by_operation = await store.search(OpsHistorySearchQuery(operation=DeviceOperation.ASPIRATE))
    assert [r for _, r in by_operation] == [aspirate]
    assert [r for _, r in await store.search(OpsHistorySearchQuery(device_name="lh_2"))] == [dispense]


async def test_concurrent_appends_all_land(store: SqliteOpsHistoryStore) -> None:
    await asyncio.gather(*[store.append("exec-1", _record(f"a{i}")) for i in range(50)])
    assert {r.action_id for r in await store.list("exec-1")} == {f"a{i}" for i in range(50)}


def _device_rows(db_path: Path) -> list[tuple[int, str]]:
    conn = sqlite3.connect(db_path)
    try:
        return list(conn.execute("SELECT record_id, device_name FROM ops_history_device ORDER BY device_name"))
    finally:
        conn.close()


async def test_a_write_saves_one_device_row_for_each_device_the_record_ran_on(
    store: SqliteOpsHistoryStore, db_path: Path,
) -> None:
    await store.append("exec-1", _record("a1", ops=[
        _aspirate("plate1", device="lh_1"), _aspirate("plate2", device="lh_1"), _aspirate("plate3", device="arm"),
    ]))
    assert [name for _, name in _device_rows(db_path)] == ["arm", "lh_1"]


async def test_search_by_device_returns_only_that_devices_records(
    store: SqliteOpsHistoryStore,
) -> None:
    on_lh1 = _record("a1", ops=[_aspirate("plate1", device="lh_1")])
    on_lh2 = _record("a2", ops=[_aspirate("plate1", device="lh_2")])
    on_both = _record("a3", ops=[_aspirate("plate1", device="lh_1"), _aspirate("plate1", device="lh_2")])
    for execution_id, record in (("exec-1", on_lh1), ("exec-2", on_lh2), ("exec-2", on_both)):
        await store.append(execution_id, record)
    found = await store.search(OpsHistorySearchQuery(device_name="lh_1"))
    assert [r.action_id for _, r in found] == ["a1", "a3"]
    assert await store.search(OpsHistorySearchQuery(device_name="no_such_device")) == []


async def test_search_by_device_reads_the_device_column_not_the_payload(
    store: SqliteOpsHistoryStore, db_path: Path,
) -> None:
    await store.append("exec-1", _record("a1", ops=[_aspirate("plate1", device="lh_1")]))
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM ops_history_device")
        conn.commit()
    finally:
        conn.close()
    assert await store.search(OpsHistorySearchQuery(device_name="lh_1")) == []
