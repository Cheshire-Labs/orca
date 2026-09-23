"""OpsHistory integration: append, filter, initial-state seeding."""
import inspect
import time

import pytest

from orca.state.ops_history import OpsHistory
from orca.state.ops_store import SYSTEM_ID
from orca.state.records import (
    AspirateDetails,
    DeviceOperation,
    DispenseDetails,
    InitialStateDetails,
    OperationRecord,
    TrackingRecord,
    TrackingSource,
)


def _record(ops: list[OperationRecord], execution_id: str = "_system") -> TrackingRecord:
    return TrackingRecord(
        execution_id=execution_id,
        action_id="a1", thread_id="t1", method_id="m1",
        source=TrackingSource.OBSERVED, timestamp=time.time(),
        operations=ops,
    )


def _op(op_type: DeviceOperation, affected: list[str], details) -> OperationRecord:
    return OperationRecord(
        operation=op_type, device_name="lh",
        affected_labware=affected, action_id="a1", thread_id="t1",
        details=details, timestamp=time.time(),
    )


class TestOpsHistory:
    @pytest.mark.asyncio
    async def test_append_record_makes_ops_visible(self) -> None:
        h = OpsHistory()
        await h.append_record(_record([
            _op(DeviceOperation.ASPIRATE, ["plate1"], AspirateDetails(labware="plate1", positions=["A1"], volumes=[50.0])),
        ]))
        ops = await h.all_operations()
        assert len(ops) == 1
        assert ops[0].operation == DeviceOperation.ASPIRATE

    @pytest.mark.asyncio
    async def test_ops_for_filters_by_affected_labware(self) -> None:
        h = OpsHistory()
        await h.append_record(_record([
            _op(DeviceOperation.ASPIRATE, ["source"], AspirateDetails(labware="source", positions=["A1"], volumes=[50.0])),
            _op(DeviceOperation.DISPENSE, ["target"], DispenseDetails(labware="target", positions=["B1"], volumes=[50.0])),
        ]))
        assert len(await h.ops_for("source")) == 1
        assert len(await h.ops_for("target")) == 1
        assert len(await h.ops_for("unknown")) == 0

    @pytest.mark.asyncio
    async def test_ops_preserve_append_order_across_records(self) -> None:
        h = OpsHistory()
        await h.append_record(_record([_op(DeviceOperation.ASPIRATE, ["p"], AspirateDetails(labware="p", positions=["A1"], volumes=[10.0]))]))
        await h.append_record(_record([_op(DeviceOperation.DISPENSE, ["p"], DispenseDetails(labware="p", positions=["A1"], volumes=[20.0]))]))
        ops = await h.ops_for("p")
        assert ops[0].operation == DeviceOperation.ASPIRATE
        assert ops[1].operation == DeviceOperation.DISPENSE

    @pytest.mark.asyncio
    async def test_append_initial_state_is_queryable(self) -> None:
        h = OpsHistory()
        await h.append_initial_state("plate1", InitialStateDetails(labware="plate1", well_volumes={"A1": 100.0}))
        ops = await h.ops_for("plate1")
        assert len(ops) == 1
        assert ops[0].operation == DeviceOperation.INITIAL_STATE
        assert ops[0].thread_id == SYSTEM_ID

    @pytest.mark.asyncio
    async def test_multi_affected_labware_in_one_op(self) -> None:
        # An aspirate-then-dispense with tips involved touches three labware.
        h = OpsHistory()
        await h.append_record(_record([
            _op(
                DeviceOperation.ASPIRATE,
                ["source", "rack"],
                AspirateDetails(labware="source", positions=["A1"], volumes=[10.0]),
            ),
        ]))
        assert len(await h.ops_for("source")) == 1
        assert len(await h.ops_for("rack")) == 1

    @pytest.mark.asyncio
    async def test_empty_history(self) -> None:
        h = OpsHistory()
        assert await h.all_operations() == []
        assert await h.ops_for("anything") == []

    def test_writes_and_reads_are_async(self) -> None:
        """Pin the async contract: every read and write path is a coroutine.

        Sync callers calling these methods without await get a coroutine
        back instead of a side effect. The previous design hand-stepped
        coroutines via _await_sync / _drive_sync_complete; both are gone.
        """
        for name in ("append_record", "append_initial_state", "records", "all_operations", "ops_for"):
            method = getattr(OpsHistory, name)
            assert inspect.iscoroutinefunction(method), f"OpsHistory.{name} must be async"
