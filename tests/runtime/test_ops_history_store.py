"""JsonlOpsHistoryStore behavior: per-execution bucketing, list, search.

The store keeps records bucketed per execution in one JSONL file each; queries
are scoped to one execution_id; search returns matching (execution_id, record)
tuples across all buckets. JSONL-specific cases (durability across reopen, the
on-disk line format, path-separator rejection, the ephemeral default) follow
the shared-contract cases.
"""
import asyncio
import gc
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from orca.state.jsonl_store import JsonlOpsHistoryStore
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


def _record(
    action_id: str = "a1",
    thread_id: str = "t1",
    method_id: str = "m1",
    ops: list[OperationRecord] | None = None,
    execution_id: str = "exec-test",
    timestamp: float | None = None,
) -> TrackingRecord:
    return TrackingRecord(
        execution_id=execution_id,
        action_id=action_id,
        thread_id=thread_id,
        method_id=method_id,
        source=TrackingSource.OBSERVED,
        timestamp=time.time() if timestamp is None else timestamp,
        operations=ops or [],
    )


def _op(
    op_type: DeviceOperation,
    affected: list[str],
    details: OperationDetails,
    device: str = "lh",
    action_id: str = "a1",
    thread_id: str = "t1",
) -> OperationRecord:
    return OperationRecord(
        operation=op_type,
        device_name=device,
        affected_labware=affected,
        action_id=action_id,
        thread_id=thread_id,
        details=details,
        timestamp=time.time(),
    )


class TestJsonlOpsHistoryStore:
    @pytest.mark.asyncio
    async def test_append_and_list_for_single_execution(self, tmp_path: Path) -> None:
        store = JsonlOpsHistoryStore(tmp_path)
        rec = _record()
        await store.append("exec-1", rec)
        records = await store.list("exec-1")
        assert records == [rec]

    @pytest.mark.asyncio
    async def test_records_bucketed_by_execution_id(self, tmp_path: Path) -> None:
        store = JsonlOpsHistoryStore(tmp_path)
        r1 = _record(action_id="a1")
        r2 = _record(action_id="a2")
        await store.append("exec-1", r1)
        await store.append("exec-2", r2)
        assert await store.list("exec-1") == [r1]
        assert await store.list("exec-2") == [r2]

    @pytest.mark.asyncio
    async def test_list_unknown_execution_returns_empty(self, tmp_path: Path) -> None:
        store = JsonlOpsHistoryStore(tmp_path)
        assert await store.list("never-existed") == []

    @pytest.mark.asyncio
    async def test_for_execution_view_lists_only_that_execution(
        self, tmp_path: Path,
    ) -> None:
        store = JsonlOpsHistoryStore(tmp_path)
        r1 = _record(action_id="a1")
        r2 = _record(action_id="a2")
        await store.append("exec-1", r1)
        await store.append("exec-2", r2)
        view = store.for_execution("exec-1")
        assert await view.list() == [r1]

    @pytest.mark.asyncio
    async def test_search_matches_by_action_id_across_executions(
        self, tmp_path: Path,
    ) -> None:
        store = JsonlOpsHistoryStore(tmp_path)
        target = _record(action_id="needle")
        other = _record(action_id="haystack")
        await store.append("exec-1", target)
        await store.append("exec-2", other)
        results = await store.search(OpsHistorySearchQuery(action_id="needle"))
        assert results == [("exec-1", target)]

    @pytest.mark.asyncio
    async def test_search_matches_by_device_operation(self, tmp_path: Path) -> None:
        store = JsonlOpsHistoryStore(tmp_path)
        asp = _record(action_id="a1", ops=[
            _op(DeviceOperation.ASPIRATE, ["plate1"],
                AspirateDetails(labware="plate1", positions=["A1"], volumes=[10.0])),
        ])
        disp = _record(action_id="a2", ops=[
            _op(DeviceOperation.DISPENSE, ["plate1"],
                DispenseDetails(labware="plate1", positions=["A1"], volumes=[10.0])),
        ])
        await store.append("exec-1", asp)
        await store.append("exec-1", disp)
        results = await store.search(
            OpsHistorySearchQuery(operation=DeviceOperation.ASPIRATE),
        )
        assert [r for _, r in results] == [asp]

    @pytest.mark.asyncio
    async def test_search_matches_by_labware_name(self, tmp_path: Path) -> None:
        store = JsonlOpsHistoryStore(tmp_path)
        plate1_op = _record(action_id="a1", ops=[
            _op(DeviceOperation.ASPIRATE, ["plate1"],
                AspirateDetails(labware="plate1", positions=["A1"], volumes=[10.0])),
        ])
        plate2_op = _record(action_id="a2", ops=[
            _op(DeviceOperation.ASPIRATE, ["plate2"],
                AspirateDetails(labware="plate2", positions=["A1"], volumes=[10.0])),
        ])
        await store.append("exec-1", plate1_op)
        await store.append("exec-2", plate2_op)
        results = await store.search(OpsHistorySearchQuery(labware_name="plate1"))
        assert results == [("exec-1", plate1_op)]

    @pytest.mark.asyncio
    async def test_search_matches_by_execution_id_filter(self, tmp_path: Path) -> None:
        store = JsonlOpsHistoryStore(tmp_path)
        r1 = _record(action_id="a1")
        r2 = _record(action_id="a2")
        await store.append("exec-1", r1)
        await store.append("exec-2", r2)
        results = await store.search(OpsHistorySearchQuery(execution_id="exec-2"))
        assert results == [("exec-2", r2)]

    @pytest.mark.asyncio
    async def test_search_with_no_filters_returns_everything(
        self, tmp_path: Path,
    ) -> None:
        store = JsonlOpsHistoryStore(tmp_path)
        r1 = _record(action_id="a1")
        r2 = _record(action_id="a2")
        await store.append("exec-1", r1)
        await store.append("exec-2", r2)
        results = await store.search(OpsHistorySearchQuery())
        assert {(eid, r.action_id) for eid, r in results} == {("exec-1", "a1"), ("exec-2", "a2")}

    @pytest.mark.asyncio
    async def test_system_sentinel_bucket_is_isolated(self, tmp_path: Path) -> None:
        """System-level seed records (sentinel '_system') don't appear under user executions."""
        store = JsonlOpsHistoryStore(tmp_path)
        seed = _record(action_id="seed")
        user = _record(action_id="user")
        await store.append("_system", seed)
        await store.append("exec-1", user)
        assert await store.list("exec-1") == [user]
        assert await store.list("_system") == [seed]

    @pytest.mark.asyncio
    async def test_records_persist_across_store_reopen(self, tmp_path: Path) -> None:
        """The durability win: a fresh store over the same dir sees prior records."""
        writer = JsonlOpsHistoryStore(tmp_path)
        rec = _record(action_id="durable")
        await writer.append("exec-1", rec)
        reopened = JsonlOpsHistoryStore(tmp_path)
        assert await reopened.list("exec-1") == [rec]

    @pytest.mark.asyncio
    async def test_append_writes_one_jsonl_line_per_record(self, tmp_path: Path) -> None:
        store = JsonlOpsHistoryStore(tmp_path)
        await store.append("exec-1", _record(action_id="a1"))
        await store.append("exec-1", _record(action_id="a2"))
        path = tmp_path / "exec-1.jsonl"
        assert path.exists()
        assert len([ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]) == 2

    @pytest.mark.asyncio
    async def test_append_rejects_execution_id_with_path_separator(
        self, tmp_path: Path,
    ) -> None:
        store = JsonlOpsHistoryStore(tmp_path)
        with pytest.raises(ValueError):
            await store.append("bad/id", _record())
        with pytest.raises(ValueError):
            await store.append("..", _record())

    @pytest.mark.asyncio
    async def test_ephemeral_store_round_trips(self) -> None:
        store = JsonlOpsHistoryStore.ephemeral()
        rec = _record(action_id="a1")
        await store.append("exec-1", rec)
        assert await store.list("exec-1") == [rec]

    @pytest.mark.asyncio
    async def test_search_orders_cross_execution_matches_chronologically(
        self, tmp_path: Path,
    ) -> None:
        """Cross-execution search is chronological (timestamp), not by filename.

        'z-exec' is appended earlier than 'a-exec', so lexicographic filename
        order would surface 'a-exec' first; the ascending-timestamp sort (the
        IOpsHistoryStore ordering contract) must surface the earlier record first.
        """
        store = JsonlOpsHistoryStore(tmp_path)
        early = _record(action_id="early", execution_id="z-exec", timestamp=100.0)
        late = _record(action_id="late", execution_id="a-exec", timestamp=200.0)
        await store.append("z-exec", early)
        await store.append("a-exec", late)
        results = await store.search(OpsHistorySearchQuery())
        assert [r.action_id for _, r in results] == ["early", "late"]

    @pytest.mark.asyncio
    async def test_list_tolerates_torn_trailing_line(self, tmp_path: Path) -> None:
        """An interrupted final append (no newline, partial JSON) is skipped,
        so prior intact records still read back."""
        store = JsonlOpsHistoryStore(tmp_path)
        await store.append("exec-1", _record(action_id="good"))
        with (tmp_path / "exec-1.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"execution_id": "exec-1", "action_id": "tor')
        records = await store.list("exec-1")
        assert [r.action_id for r in records] == ["good"]

    @pytest.mark.asyncio
    async def test_list_raises_on_corruption_before_last_line(
        self, tmp_path: Path,
    ) -> None:
        """Only the torn TRAILING line is tolerated; earlier corruption raises."""
        with (tmp_path / "exec-1.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("not valid json\n")
        store = JsonlOpsHistoryStore(tmp_path)
        await store.append("exec-1", _record(action_id="good"))
        with pytest.raises(ValidationError):
            await store.list("exec-1")

    @pytest.mark.asyncio
    async def test_search_across_executions_after_reopen(
        self, tmp_path: Path,
    ) -> None:
        """The glob-all-files branch reads prior on-disk buckets a fresh store
        never appended in-process."""
        writer = JsonlOpsHistoryStore(tmp_path)
        await writer.append("exec-1", _record(action_id="a1", execution_id="exec-1"))
        await writer.append("exec-2", _record(action_id="a2", execution_id="exec-2"))
        reopened = JsonlOpsHistoryStore(tmp_path)
        results = await reopened.search(OpsHistorySearchQuery())
        assert {(eid, r.action_id) for eid, r in results} == {
            ("exec-1", "a1"), ("exec-2", "a2"),
        }

    @pytest.mark.asyncio
    async def test_concurrent_appends_to_one_execution_keep_all_lines_intact(
        self, tmp_path: Path,
    ) -> None:
        """N appends fired concurrently land as N intact, parseable lines.

        Each append offloads to a worker thread; the archive lock serializes
        the writes so none interleave into a torn line.
        """
        store = JsonlOpsHistoryStore(tmp_path)
        n = 50
        await asyncio.gather(
            *[store.append("exec-1", _record(action_id=f"a{i}")) for i in range(n)]
        )
        records = await store.list("exec-1")
        assert {r.action_id for r in records} == {f"a{i}" for i in range(n)}
        lines = [
            ln for ln in (tmp_path / "exec-1.jsonl").read_text(
                encoding="utf-8"
            ).splitlines() if ln
        ]
        assert len(lines) == n

    @pytest.mark.asyncio
    async def test_ephemeral_store_cleans_up_temp_dir_when_dropped(self) -> None:
        """Dropping the ephemeral store removes its temp dir (no leak).

        TemporaryDirectory cleans via a finalizer when the store, which holds
        the only reference to it, is garbage-collected.
        """
        store = JsonlOpsHistoryStore.ephemeral()
        await store.append("exec-1", _record(action_id="a1"))
        assert store._tmp is not None
        tmp_dir = Path(store._tmp.name)
        assert tmp_dir.exists()
        del store
        gc.collect()
        assert not tmp_dir.exists()
