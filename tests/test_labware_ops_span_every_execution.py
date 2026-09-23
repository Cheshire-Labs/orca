"""A labware's ops read the same however many executions it has lived through,
and however many times the runtime has restarted.

Replaces the per-instance ops-bucket tests. Buckets were an approximation of
this guarantee that a restart could sever: each execution bound its own source
onto the live instance, so a labware whose instance was rebuilt read only what
something had re-bound. The store already held every bucket, so reading it
directly is both simpler and the only version that survives a restart.

What is still worth pinning is the ordering contract, because it is what makes
one merged read from many buckets trustworthy.
"""

import time

from orca.resource_models.labware import LabwareInstance
from orca.state.contents import LabwareContentsLedger
from orca.state.ops_history import OpsHistory
from orca.state.records import (
    DeviceOperation,
    GenericOperationDetails,
    OperationRecord,
    TrackingRecord,
    TrackingSource,
)
from orca.state.jsonl_store import JsonlOpsHistoryStore


def _record(name: str, ts: float, seq: int | None = None) -> OperationRecord:
    kwargs = {} if seq is None else {"sequence": seq}
    return OperationRecord(
        operation=DeviceOperation.SHAKE,
        device_name="shaker_1",
        affected_labware=[name],
        action_id="act-1",
        thread_id="thr-1",
        details=GenericOperationDetails(command="shake", args_repr=""),
        timestamp=ts,
        **kwargs,
    )


async def _write(
    history: OpsHistory, execution_id: str, op: OperationRecord,
) -> None:
    await history.for_execution(execution_id).append_record(TrackingRecord(
        execution_id=execution_id, action_id="act-1", thread_id="thr-1",
        method_id=None, source=TrackingSource.OBSERVED, timestamp=op.timestamp,
        operations=[op],
    ))


def _bound_instance(history: OpsHistory) -> LabwareInstance:
    instance = LabwareInstance("plate_x", "96_well")
    instance.bind_contents(LabwareContentsLedger(history))
    return instance


class TestOpsReadEveryExecution:
    async def test_ops_from_several_executions_read_as_one_history(self) -> None:
        history = OpsHistory(store=JsonlOpsHistoryStore.ephemeral())
        instance = _bound_instance(history)
        t0 = time.time()
        await _write(history, "run_1", _record(instance.name, t0))
        await _write(history, "run_2", _record(instance.name, t0 + 300.0))

        ops = await instance.ops()
        assert [op.timestamp for op in ops] == [t0, t0 + 300.0]

    async def test_a_rebuilt_instance_reads_what_the_first_one_did(self) -> None:
        """The restart shape: nothing re-binds, and nothing is lost."""
        history = OpsHistory(store=JsonlOpsHistoryStore.ephemeral())
        first = _bound_instance(history)
        await _write(history, "run_1", _record(first.name, time.time()))

        rebuilt = LabwareInstance(
            "plate_x", "96_well", instance_id=first.id, name=first.name,
        )
        rebuilt.bind_contents(LabwareContentsLedger(history))
        assert len(await rebuilt.ops()) == 1

    async def test_an_unbound_instance_reads_nothing(self) -> None:
        assert await LabwareInstance("plate_x", "96_well").ops() == []


class TestOpsOrdering:
    """Equal timestamps happen: a coarse clock collides an operator SET_VOLUME
    with a near-simultaneous aspirate. Creation order settles them."""

    async def test_equal_timestamps_order_by_creation_sequence(self) -> None:
        history = OpsHistory(store=JsonlOpsHistoryStore.ephemeral())
        instance = _bound_instance(history)
        ts = time.time()
        # Written in the opposite order to their creation: a timestamp-only
        # stable sort would leave them inverted.
        await _write(history, "run_2", _record(instance.name, ts, seq=2))
        await _write(history, "run_1", _record(instance.name, ts, seq=1))

        assert [op.sequence for op in await instance.ops()] == [1, 2]

    def test_sequence_auto_increments_in_creation_order(self) -> None:
        first = _record("plate_x", 1_700_000_000.0)
        second = _record("plate_x", 1_700_000_000.0)
        assert first.sequence < second.sequence
