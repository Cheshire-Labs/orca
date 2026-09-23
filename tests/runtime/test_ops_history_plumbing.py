"""execution_id flows through the workflow factory chain into OpsHistory.

The seed-bucket and append-bucket must agree: when WorkflowFactory builds an
entry thread under execution_id E, the labware's INITIAL_STATE seed lands in
bucket E and TrackingContext.store_record(record, execution_id=E) lands in
the same bucket.
"""
import time
from pathlib import Path

import pytest

from orca.state.ops_history import OpsHistory
from orca.state.records import (
    DeviceOperation,
    InitialStateDetails,
    TrackingRecord,
    TrackingSource,
)
from orca.resource_models.tracking_context import TrackingContext
from orca.resource_models.tracking_observer import NullTrackingObserver
from orca.state.jsonl_store import JsonlOpsHistoryStore
from orca.state.ops_store import SYSTEM_ID


def _record(action_id: str = "a1", execution_id: str = "exec-test") -> TrackingRecord:
    return TrackingRecord(
        execution_id=execution_id,
        action_id=action_id, thread_id="t1", method_id="m1",
        source=TrackingSource.OBSERVED, timestamp=time.time(),
        operations=[],
    )


class TestOpsHistoryExecutionScoping:
    @pytest.mark.asyncio
    async def test_for_execution_isolates_writes(self, tmp_path: Path) -> None:
        """Two OpsHistory views over the same store write to distinct buckets."""
        store = JsonlOpsHistoryStore(tmp_path)
        history = OpsHistory(store=store)
        e1 = history.for_execution("exec-1")
        e2 = history.for_execution("exec-2")
        await e1.append_record(_record("a1"))
        await e2.append_record(_record("a2"))
        assert [r.action_id for r in await store.list("exec-1")] == ["a1"]
        assert [r.action_id for r in await store.list("exec-2")] == ["a2"]

    @pytest.mark.asyncio
    async def test_default_history_uses_system_sentinel_bucket(
        self, tmp_path: Path,
    ) -> None:
        """Bare OpsHistory() writes to ``_system`` so test harnesses keep working."""
        store = JsonlOpsHistoryStore(tmp_path)
        history = OpsHistory(store=store)
        await history.append_record(_record("a1"))
        assert [r.action_id for r in await store.list(SYSTEM_ID)] == ["a1"]

    @pytest.mark.asyncio
    async def test_initial_state_seed_lands_in_for_execution_bucket(
        self, tmp_path: Path,
    ) -> None:
        """Seeding a labware via for_execution(eid) writes INITIAL_STATE to bucket eid."""
        store = JsonlOpsHistoryStore(tmp_path)
        history = OpsHistory(store=store)
        view = history.for_execution("exec-1")
        await view.append_initial_state(
            "plate1",
            InitialStateDetails(labware="plate1", well_volumes={"A1": 100.0}),
        )
        records = await store.list("exec-1")
        assert len(records) == 1
        op = records[0].operations[0]
        assert op.operation == DeviceOperation.INITIAL_STATE
        assert "plate1" in op.affected_labware
        # System bucket untouched.
        assert await store.list(SYSTEM_ID) == []


class TestTrackingContextRoutesByExecutionId:
    @pytest.mark.asyncio
    async def test_store_record_uses_explicit_execution_id(
        self, tmp_path: Path,
    ) -> None:
        """Action execution call sites pass the workflow's execution_id."""
        store = JsonlOpsHistoryStore(tmp_path)
        # Bind context to system bucket; action callers override per-write.
        ctx = TrackingContext(
            observer=NullTrackingObserver(),
            ops_history=OpsHistory(store=store),
        )
        await ctx.store_record(_record("a1"), execution_id="exec-X")
        records = await store.list("exec-X")
        assert [r.action_id for r in records] == ["a1"]

    @pytest.mark.asyncio
    async def test_store_record_default_falls_back_to_bound_execution(
        self, tmp_path: Path,
    ) -> None:
        """Legacy callers that pass only the record use the bound bucket."""
        store = JsonlOpsHistoryStore(tmp_path)
        ctx = TrackingContext(
            observer=NullTrackingObserver(),
            ops_history=OpsHistory(store=store, execution_id="exec-A"),
        )
        await ctx.store_record(_record("a1"))  # no execution_id arg
        records = await store.list("exec-A")
        assert [r.action_id for r in records] == ["a1"]
