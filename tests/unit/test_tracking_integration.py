"""Integration tests for the tracking chain: observer -> context -> history -> projection."""
import time

import pytest

from orca.events.execution_context import MethodExecutionContext
from orca.plugins.declared_tracking_observer import DeclaredTrackingObserver
from orca.state.projections import (
    sample_events_at,
    tips_present,
    tips_used,
    well_volume,
)
from orca.state.ops_history import OpsHistory
from orca.state.records import (
    AspirateDetails,
    DeclaredTracking,
    DeclaredVolumeTransfer,
    DeviceOperation,
    DispenseDetails,
    InitialStateDetails,
    OperationRecord,
    ShakeDetails,
    TrackingRecord,
    TrackingSource,
)
from orca.resource_models.tracking_context import TrackingContext
from orca.resource_models.tracking_observer import NullTrackingObserver


def _make_context(method_id: str = "m1", execution_id: str = "wf-1") -> MethodExecutionContext:
    return MethodExecutionContext(
        execution_id=execution_id,
        workflow_name="test_workflow",
        method_id=method_id,
        method_name="test_method",
    )


def _op(operation: DeviceOperation, affected: list[str], details, action_id: str = "a1") -> OperationRecord:
    return OperationRecord(
        operation=operation,
        device_name="lh",
        affected_labware=affected,
        action_id=action_id,
        thread_id="t1",
        details=details,
        timestamp=time.time(),
    )


def _record(
    ops: list[OperationRecord],
    action_id: str = "a1",
    execution_id: str = "_system",
) -> TrackingRecord:
    return TrackingRecord(
        execution_id=execution_id,
        action_id=action_id, thread_id="t1", method_id="m1",
        source=TrackingSource.OBSERVED, timestamp=time.time(),
        operations=ops,
    )


class TestTrackingContextRecordStore:
    @pytest.mark.asyncio
    async def test_store_record_writes_to_ops_history(self) -> None:
        history = OpsHistory()
        ctx = TrackingContext(observer=NullTrackingObserver(), ops_history=history)
        await ctx.store_record(_record([]))
        assert len(await history.records()) == 1

    @pytest.mark.asyncio
    async def test_multiple_stored_records_accumulate_in_history(self) -> None:
        history = OpsHistory()
        ctx = TrackingContext(observer=NullTrackingObserver(), ops_history=history)
        await ctx.store_record(_record([], action_id="a1"))
        await ctx.store_record(_record([], action_id="a2"))
        assert [r.action_id for r in await history.records()] == ["a1", "a2"]


class TestObserverToOpsHistoryToProjection:
    """Declared observer output becomes queryable via projections."""

    @pytest.mark.asyncio
    async def test_declared_tips_used_become_queryable_via_projections(self) -> None:
        history = OpsHistory()
        await history.append_initial_state(
            "tips_96", InitialStateDetails(labware="tips_96", tip_positions_present=["A1", "A2", "A3"]),
        )
        observer = DeclaredTrackingObserver()
        declares = DeclaredTracking(tips_used={"tips_96": ["A1", "A2"]})

        record = observer.process_operations([], _make_context(), "a1", "t1", declares=declares)
        assert record is not None
        await history.append_record(record)

        ops = await history.ops_for("tips_96")
        assert tips_present(ops, "tips_96") == {"A3"}
        assert tips_used(ops, "tips_96") == {"A1", "A2"}

    @pytest.mark.asyncio
    async def test_declared_volume_transfer_becomes_queryable_per_side(self) -> None:
        """Paired aspirate+dispense ops are queryable on both source and target
        via ops_for; well_volume reflects both sides of the transfer."""
        history = OpsHistory()
        await history.append_initial_state(
            "src", InitialStateDetails(labware="src", well_volumes={"A1": 100.0}),
        )
        await history.append_initial_state(
            "dst", InitialStateDetails(labware="dst", well_volumes={"B1": 0.0}),
        )
        observer = DeclaredTrackingObserver()
        declares = DeclaredTracking(volume_transferred=[
            DeclaredVolumeTransfer(
                source="src", target="dst", volume_ul=50.0,
                source_wells=["A1"], target_wells=["B1"],
            ),
        ])

        record = observer.process_operations([], _make_context(), "a1", "t1", declares=declares)
        assert record is not None
        await history.append_record(record)

        assert well_volume(await history.ops_for("src"), "src", "A1") == 50.0
        assert well_volume(await history.ops_for("dst"), "dst", "B1") == 50.0


class TestObserverPassthroughWithoutDeclares:
    """Closes the observed-records data-loss path end-to-end.

    The interpreter-populated _operation_log on an action with no DeclaredTracking
    must still reach ops_history. Tests the chain observer.process_operations ->
    ops_history.append_record -> ops_for(labware), with the observer's new
    OBSERVED-passthrough emitting a TrackingRecord even when declares is None.
    """

    @pytest.mark.asyncio
    async def test_observed_aspirate_records_reach_ops_history_without_declares(self) -> None:
        history = OpsHistory()
        observer = DeclaredTrackingObserver()
        observed_ops = [
            _op(DeviceOperation.ASPIRATE, ["plate_a"],
                AspirateDetails(labware="plate_a", positions=["A1"], volumes=[100.0]), action_id="a1"),
            _op(DeviceOperation.DISPENSE, ["plate_b"],
                DispenseDetails(labware="plate_b", positions=["B1"], volumes=[100.0]), action_id="a1"),
        ]
        record = observer.process_operations(observed_ops, _make_context(), "a1", thread_id="t1")
        assert record is not None
        assert record.source == TrackingSource.OBSERVED
        await history.append_record(record)

        ops_a = await history.ops_for("plate_a")
        assert [o.operation for o in ops_a] == [DeviceOperation.ASPIRATE]
        assert isinstance(ops_a[0].details, AspirateDetails)
        assert ops_a[0].details.volumes == [100.0]

        ops_b = await history.ops_for("plate_b")
        assert [o.operation for o in ops_b] == [DeviceOperation.DISPENSE]
        assert isinstance(ops_b[0].details, DispenseDetails)

    @pytest.mark.asyncio
    async def test_mixed_observed_and_driver_observed_ops_pass_through_without_declares(self) -> None:
        """LiquidHandlerInterpreter.interpret_driver_state may extend the action's
        operation log with DRIVER_OBSERVED records alongside the OBSERVED action-
        derived record produced by ActionBodyLocationAction.execute. Both must
        survive the observer when declares is None."""
        history = OpsHistory()
        observer = DeclaredTrackingObserver()
        observed = _op(DeviceOperation.ASPIRATE, ["plate_a"],
                       AspirateDetails(labware="plate_a", positions=["A1"], volumes=[50.0]),
                       action_id="a1")
        driver_observed = OperationRecord(
            operation=DeviceOperation.INITIAL_STATE, device_name="lh",
            affected_labware=["plate_a"], action_id="a1", thread_id="t1",
            details=InitialStateDetails(labware="plate_a", well_volumes={"A1": 50.0}),
            timestamp=time.time(), source=TrackingSource.DRIVER_OBSERVED,
        )
        record = observer.process_operations(
            [observed, driver_observed], _make_context(), "a1", thread_id="t1",
        )
        assert record is not None
        await history.append_record(record)

        ops = await history.ops_for("plate_a")
        sources = [o.source for o in ops]
        assert TrackingSource.OBSERVED in sources
        assert TrackingSource.DRIVER_OBSERVED in sources

    @pytest.mark.asyncio
    async def test_null_tracking_observer_still_returns_none_with_ops_and_no_declares(self) -> None:
        """The passthrough behavior is scoped to DeclaredTrackingObserver.
        NullTrackingObserver must continue dropping every input regardless of
        operations or declares (it is the explicit no-op observer)."""
        observer = NullTrackingObserver()
        ops = [_op(DeviceOperation.ASPIRATE, ["plate_a"],
                   AspirateDetails(labware="plate_a", positions=["A1"], volumes=[10.0]))]
        assert observer.process_operations(ops, _make_context(), "a1", "t1") is None


class TestOpsHistoryEndToEnd:
    """End-to-end OpsHistory + projection scenarios."""

    @pytest.mark.asyncio
    async def test_shake_op_is_recorded_and_queryable(self) -> None:
        """Non-pipetting ops on a labware are tracked and retrievable via
        ops_for(labware_name)."""
        history = OpsHistory()
        await history.append_record(_record([
            _op(DeviceOperation.SHAKE, ["plate_1"],
                ShakeDetails(speed_rpm=300.0, duration_s=120.0)),
        ]))
        ops = await history.ops_for("plate_1")
        assert len(ops) == 1
        assert ops[0].operation == DeviceOperation.SHAKE
        assert isinstance(ops[0].details, ShakeDetails)
        assert ops[0].details.speed_rpm == 300.0

    @pytest.mark.asyncio
    async def test_multi_step_provenance_chain(self) -> None:
        """Walking backwards across two transfer steps (src -> intermediate ->
        final) recovers every touched well on every labware via
        sample_events_at."""
        history = OpsHistory()
        # Step 1: src A1 -> intermediate B1
        await history.append_record(_record([
            _op(DeviceOperation.ASPIRATE, ["src"],
                AspirateDetails(labware="src", positions=["A1"], volumes=[50.0]), action_id="a1"),
            _op(DeviceOperation.DISPENSE, ["intermediate"],
                DispenseDetails(labware="intermediate", positions=["B1"], volumes=[50.0]), action_id="a1"),
        ], action_id="a1"))
        # Step 2: intermediate B1 -> final C1
        await history.append_record(_record([
            _op(DeviceOperation.ASPIRATE, ["intermediate"],
                AspirateDetails(labware="intermediate", positions=["B1"], volumes=[50.0]), action_id="a2"),
            _op(DeviceOperation.DISPENSE, ["final"],
                DispenseDetails(labware="final", positions=["C1"], volumes=[50.0]), action_id="a2"),
        ], action_id="a2"))

        final_events = sample_events_at(await history.ops_for("final"), "final", "C1")
        assert len(final_events) == 1 and final_events[0].operation == DeviceOperation.DISPENSE

        # Intermediate B1 was both dispense target (step 1) and aspirate source (step 2).
        inter_events = sample_events_at(await history.ops_for("intermediate"), "intermediate", "B1")
        assert [e.operation for e in inter_events] == [DeviceOperation.DISPENSE, DeviceOperation.ASPIRATE]

        src_events = sample_events_at(await history.ops_for("src"), "src", "A1")
        assert len(src_events) == 1 and src_events[0].operation == DeviceOperation.ASPIRATE

    @pytest.mark.asyncio
    async def test_cumulative_volume_across_multiple_dispenses(self) -> None:
        """Sequential dispenses into one well accumulate through well_volume."""
        history = OpsHistory()
        await history.append_initial_state(
            "dst", InitialStateDetails(labware="dst", well_volumes={"B1": 0.0}),
        )
        for i, vol in enumerate([50.0, 30.0, 20.0]):
            await history.append_record(_record([
                _op(DeviceOperation.ASPIRATE, ["src"],
                    AspirateDetails(labware="src", positions=["A1"], volumes=[vol]), action_id=f"a{i}"),
                _op(DeviceOperation.DISPENSE, ["dst"],
                    DispenseDetails(labware="dst", positions=["B1"], volumes=[vol]), action_id=f"a{i}"),
            ], action_id=f"a{i}"))

        assert well_volume(await history.ops_for("dst"), "dst", "B1") == 100.0


class TestPerChannelFaultPipelineEndToEnd:
    """End-to-end pipeline canary for per-channel partial-failure attribution.

    A customer-facing gap: an aspirate that errored mid-call left
    ops_history empty, so scientists
    could not see "which wells were transferred / which were not." This
    test pins the full producing -> interpreting -> observing -> persisting
    chain end-to-end.

    Wire shape under test (matches cheshire-drivers ``PartialFault``-armed
    sim driver output): a ``LabwareStateResponse`` with
    ``per_channel_errors=[ChannelError(channel_id=2, well_position="C1",
    ...)]`` for an 8-channel aspirate request. The interpreter emits one
    record per well; the observer (with ``declares=None``, mirroring the
    Hamilton-SMC workflow which has no DeclaredTracking annotation)
    passes them through as TrackingRecord(source=OBSERVED); the ops_history
    captures them; querying by labware returns 7 confirmed_transferred +
    1 definitely_not_transferred with the firmware error code attached.

    Without the observer passthrough, declares=None would drop the records
    silently. Without the per-channel interpreter, partial-failure responses
    would yield no per-well shape. Both fixes must be in place for this
    test to pass.
    """

    @pytest.mark.asyncio
    async def test_partial_failure_aspirate_records_reach_ops_history_with_per_well_certainty(
        self,
    ) -> None:
        from cheshire_drivers.liquid_handler_models import (
            ChannelError,
            LabwareStateResponse,
        )
        from orca.plugins.liquid_handler_interpreter import LiquidHandlerInterpreter

        class _FakeWell:
            def __init__(self, parent_name: str, identifier: str) -> None:
                self.parent_name = parent_name
                self.identifier = identifier
                self.resource_name = parent_name
                self.position = identifier

        # 8-channel aspirate request: A1..H1 of plate_X, 100 uL each.
        positions = ["A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1"]
        wells = [_FakeWell("plate_X", p) for p in positions]
        volumes = [100.0] * 8

        # Sim driver returns partial-failure: channel 2 (well C1) errored.
        # All other channels completed normally. This mirrors what
        # PLRLiquidHandlerWrapper produces from a Hamilton ChannelizedError
        # via build_aspirate_partial_failure in cheshire-drivers.
        result = LabwareStateResponse(
            success=False,
            per_channel_errors=[
                ChannelError(
                    channel_id=2,
                    well_position="C1",
                    labware="plate_X",
                    attempted_volume=100.0,
                    error_code="HamiltonE100",
                    error_message="channel 2: pressure deviation",
                ),
            ],
        )

        # Interpreter consumes the response and emits per-well records.
        interpreter = LiquidHandlerInterpreter()
        records = interpreter.interpret_per_channel_outcomes(
            command="aspirate",
            args=(wells, volumes),
            kwargs={},
            result=result,
            device_name="mlstar_1",
            affected_labware=["plate_X"],
            affected_labware_ids=["plate_X-id"],
            action_id="a-1",
            thread_id="t-1",
        )
        assert len(records) == 8

        # Observer pass-through (declares=None mirrors the Hamilton SMC
        # workflow shape; the passthrough lets these records reach the
        # persistence layer).
        observer = DeclaredTrackingObserver()
        ctx = _make_context(method_id="m-1")
        tracking_record = observer.process_operations(
            operations=records,
            execution_context=ctx,
            action_id="a-1",
            declares=None,
            thread_id="t-1",
        )
        assert tracking_record is not None
        assert tracking_record.source == TrackingSource.OBSERVED

        # OpsHistory captures the record; querying surfaces the per-well shape.
        history = OpsHistory()
        await history.append_record(tracking_record)
        ops = await history.ops_for("plate_X")
        assert len(ops) == 8

        # Partition by certainty: 7 confirmed_transferred + 1 not_transferred.
        confirmed = [
            op for op in ops
            if isinstance(op.details, AspirateDetails)
            and op.details.certainty == "confirmed_transferred"
        ]
        not_transferred = [
            op for op in ops
            if isinstance(op.details, AspirateDetails)
            and op.details.certainty == "definitely_not_transferred"
        ]
        assert len(confirmed) == 7
        assert len(not_transferred) == 1

        # Failed channel: well C1, volumes=[0], error_code wired through.
        (failed_op,) = not_transferred
        assert isinstance(failed_op.details, AspirateDetails)
        assert failed_op.details.positions == ["C1"]
        assert failed_op.details.volumes == [0.0]
        assert failed_op.details.error_code == "HamiltonE100"

        # Successful channels: requested volume, no error_code.
        confirmed_positions = sorted(
            d.details.positions[0]
            for d in confirmed
            if isinstance(d.details, AspirateDetails)
        )
        assert confirmed_positions == ["A1", "B1", "D1", "E1", "F1", "G1", "H1"]
        for op in confirmed:
            assert isinstance(op.details, AspirateDetails)
            assert op.details.volumes == [100.0]
            assert op.details.error_code is None
