"""OpsHistoryFacade routes reads through the IOpsHistoryStore."""
import time
from pathlib import Path

import pytest

from orca.state.records import (
    TrackingRecord,
    TrackingSource,
)
from orca.runtime.facades.ops_history import OpsHistoryFacade
from orca.state.jsonl_store import JsonlOpsHistoryStore
from orca.state.ops_store import OpsHistorySearchQuery


def _record(action_id: str = "a1", execution_id: str = "exec-1") -> TrackingRecord:
    return TrackingRecord(
        execution_id=execution_id,
        action_id=action_id, thread_id="t1", method_id="m1",
        source=TrackingSource.OBSERVED, timestamp=time.time(),
        operations=[],
    )


class TestOpsHistoryFacade:
    @pytest.mark.asyncio
    async def test_list_returns_only_target_execution_records(
        self, tmp_path: Path,
    ) -> None:
        store = JsonlOpsHistoryStore(tmp_path)
        await store.append("exec-1", _record("a1", "exec-1"))
        await store.append("exec-1", _record("a2", "exec-1"))
        await store.append("exec-2", _record("a3", "exec-2"))
        facade = OpsHistoryFacade(store)
        result = await facade.list("exec-1")
        # Pin full record shape: action_id ordering, execution_id filter,
        # source/thread_id/method_id metadata round-trip from the factory.
        assert [r.action_id for r in result] == ["a1", "a2"]
        assert all(r.execution_id == "exec-1" for r in result)
        assert all(r.thread_id == "t1" for r in result)
        assert all(r.method_id == "m1" for r in result)
        assert all(r.source == TrackingSource.OBSERVED for r in result)
        assert all(r.operations == [] for r in result)

    @pytest.mark.asyncio
    async def test_search_routes_through_store(self, tmp_path: Path) -> None:
        store = JsonlOpsHistoryStore(tmp_path)
        await store.append("exec-1", _record("needle", "exec-1"))
        await store.append("exec-2", _record("hay", "exec-2"))
        facade = OpsHistoryFacade(store)
        result = await facade.search(OpsHistorySearchQuery(action_id="needle"))
        assert [(eid, r.action_id) for eid, r in result] == [("exec-1", "needle")]
