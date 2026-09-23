"""DeclaredTrackingObserver produces typed OperationRecords that projections consume."""
from orca.events.execution_context import MethodExecutionContext
from orca.plugins.declared_tracking_observer import DeclaredTrackingObserver
from orca.state.records import (
    AspirateDetails,
    DeclaredTracking,
    DeclaredVolumeTransfer,
    DeviceOperation,
    DispenseDetails,
    GenericOperationDetails,
    OperationRecord,
    TipPickUpDetails,
    TrackingSource,
    WellUsageDetails,
)
from orca.resource_models.tracking_observer import NullTrackingObserver


def _make_context() -> MethodExecutionContext:
    return MethodExecutionContext(
        execution_id="wf-1",
        workflow_name="test_workflow",
        method_id="m-1",
        method_name="test_method",
    )


class TestNullTrackingObserver:
    def test_returns_none(self) -> None:
        observer = NullTrackingObserver()
        assert observer.process_operations([], _make_context(), "a1", "t1") is None

    def test_returns_none_with_declares(self) -> None:
        observer = NullTrackingObserver()
        declares = DeclaredTracking(tips_used={"tips": ["A1"]})
        assert observer.process_operations([], _make_context(), "a1", "t1", declares=declares) is None


class TestDeclaredTrackingObserver:
    def test_returns_none_without_declares_and_no_operations(self) -> None:
        observer = DeclaredTrackingObserver()
        assert observer.process_operations([], _make_context(), "a1", "t1") is None

    def test_emits_observed_record_without_declares_when_operations_present(self) -> None:
        """Closes the observed-records data-loss gap.

        When an @orca.action calls aspirate/dispense, LiquidHandlerInterpreter
        populates _operation_log with AspirateDetails / DispenseDetails ops.
        Without DeclaredTracking on the action, those observed records
        previously fell off the cliff because the observer returned None.
        Now they pass through into a TrackingRecord(source=OBSERVED).
        """
        observer = DeclaredTrackingObserver()
        upstream_ops = [
            OperationRecord(
                operation=DeviceOperation.ASPIRATE, device_name="mlstar_1",
                affected_labware=["plate_a"], action_id="a1", thread_id="t-1",
                details=AspirateDetails(labware="plate_a", positions=["A1"], volumes=[100.0]),
                timestamp=0.0,
            ),
            OperationRecord(
                operation=DeviceOperation.DISPENSE, device_name="mlstar_1",
                affected_labware=["plate_b"], action_id="a1", thread_id="t-1",
                details=DispenseDetails(labware="plate_b", positions=["B1"], volumes=[100.0]),
                timestamp=0.0,
            ),
        ]
        result = observer.process_operations(upstream_ops, _make_context(), "a1", thread_id="t-1")
        assert result is not None
        assert result.source == TrackingSource.OBSERVED
        assert result.action_id == "a1"
        assert result.thread_id == "t-1"
        assert result.method_id == "m-1"
        assert result.execution_id == "wf-1"
        assert len(result.operations) == 2
        assert result.operations[0].operation == DeviceOperation.ASPIRATE
        assert result.operations[1].operation == DeviceOperation.DISPENSE

    def test_produces_record_with_declares(self) -> None:
        observer = DeclaredTrackingObserver()
        declares = DeclaredTracking(
            tips_used={"tips_96": ["A1", "A2"]},
            volume_transferred=[
                DeclaredVolumeTransfer(
                    source="plate_a", target="plate_b", volume_ul=50.0,
                    source_wells=["A1"], target_wells=["B1"],
                ),
            ],
        )
        upstream_ops = [
            OperationRecord(
                operation=DeviceOperation.RUN_PROTOCOL, device_name="lh_1",
                affected_labware=["plate_a", "plate_b", "tips_96"],
                action_id="a1", thread_id="t1",
                details=GenericOperationDetails(command="run_protocol", args_repr="('mix.pro',)"),
                timestamp=0.0,
            ),
        ]

        result = observer.process_operations(upstream_ops, _make_context(), "a1", "t1", declares=declares)
        assert result is not None
        assert result.source == TrackingSource.DECLARED
        assert result.action_id == "a1"
        assert result.method_id == "m-1"
        assert result.execution_id == "wf-1"

        # Upstream ops pass through, then declared ops are appended.
        assert result.operations[0].operation == DeviceOperation.RUN_PROTOCOL

        aspirates = [op for op in result.operations if op.operation == DeviceOperation.ASPIRATE]
        dispenses = [op for op in result.operations if op.operation == DeviceOperation.DISPENSE]
        assert len(aspirates) == 1
        assert len(dispenses) == 1
        asp, disp = aspirates[0], dispenses[0]

        assert isinstance(asp.details, AspirateDetails)
        assert asp.details.labware == "plate_a"
        assert asp.details.positions == ["A1"]
        assert asp.details.volumes == [50.0]
        assert asp.affected_labware == ["plate_a"]

        assert isinstance(disp.details, DispenseDetails)
        assert disp.details.labware == "plate_b"
        assert disp.details.positions == ["B1"]
        assert disp.details.volumes == [50.0]
        assert disp.affected_labware == ["plate_b"]

        # Pair linkage: both sides carry the same non-None group_id so downstream
        # consumers can reconstruct the transfer as a unit.
        assert asp.group_id is not None
        assert asp.group_id == disp.group_id

        pickups = [op for op in result.operations if op.operation == DeviceOperation.PICK_UP_TIPS]
        assert len(pickups) == 1
        pickup_details = pickups[0].details
        assert isinstance(pickup_details, TipPickUpDetails)
        assert pickup_details.tip_rack == "tips_96"
        assert pickup_details.positions == ["A1", "A2"]
        assert pickups[0].affected_labware == ["tips_96"]

    def test_none_wells_become_empty_list(self) -> None:
        observer = DeclaredTrackingObserver()
        declares = DeclaredTracking(
            volume_transferred=[
                DeclaredVolumeTransfer(source="a", target="b", volume_ul=10.0),
            ],
        )
        result = observer.process_operations([], _make_context(), "a1", "t1", declares=declares)
        assert result is not None

        aspirates = [op for op in result.operations if op.operation == DeviceOperation.ASPIRATE]
        dispenses = [op for op in result.operations if op.operation == DeviceOperation.DISPENSE]
        assert len(aspirates) == 1 and len(dispenses) == 1

        assert isinstance(aspirates[0].details, AspirateDetails)
        assert aspirates[0].details.positions == []
        assert aspirates[0].details.volumes == []

        assert isinstance(dispenses[0].details, DispenseDetails)
        assert dispenses[0].details.positions == []
        assert dispenses[0].details.volumes == []

    def test_wells_used_emits_well_usage_op(self) -> None:
        """wells_used declares a closed-protocol action's touched wells;
        observer emits WELL_USAGE so ops_for(labware) exposes them."""
        observer = DeclaredTrackingObserver()
        declares = DeclaredTracking(wells_used={"plate_a": ["A1", "B2", "C3"]})

        result = observer.process_operations([], _make_context(), "a1", "t1", declares=declares)
        assert result is not None

        well_usage_ops = [op for op in result.operations if op.operation == DeviceOperation.WELL_USAGE]
        assert len(well_usage_ops) == 1
        details = well_usage_ops[0].details
        assert isinstance(details, WellUsageDetails)
        assert details.labware == "plate_a"
        assert details.positions == ["A1", "B2", "C3"]

    def test_thread_id_propagates_to_synthesized_ops_and_record(self) -> None:
        """Operator-experience fix: every ops_history record landed with
        ``thread_id=""``. The
        DeclaredTrackingObserver synthesized records without threading
        the action's owning thread_id through. The observer now accepts
        ``thread_id`` as a kwarg; ``executable_location_action`` passes
        the resolved id from ``participating_thread_ids``.
        """
        observer = DeclaredTrackingObserver()
        declares = DeclaredTracking(
            wells_used={"plate_a": ["A1"]},
            tips_used={"tips_96": ["A1"]},
            volume_transferred=[
                DeclaredVolumeTransfer(
                    source="plate_a", target="plate_b", volume_ul=10.0,
                    source_wells=["A1"], target_wells=["B1"],
                ),
            ],
        )

        result = observer.process_operations(
            [], _make_context(), "a1", declares=declares, thread_id="t-42",
        )
        assert result is not None
        assert result.thread_id == "t-42"
        # Every synthesized op carries the same thread id; absent the
        # kwarg they would all have landed empty.
        assert all(op.thread_id == "t-42" for op in result.operations)
