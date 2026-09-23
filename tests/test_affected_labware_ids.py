"""Ops-history surfaces the canonical labware UUID alongside the name.

`OperationRecord.affected_labware` carries human-readable names; the journey
and history lookups key on the canonical UUID. Without a UUID in the record
the operator has nothing copy-pasteable. These pin that the dispatch path
populates `affected_labware_ids` parallel to `affected_labware`, and that the
declared-tracking observer does the same from the resolved instance.
"""
import time

from orca.cli.ops_history import _affected_ids
from orca.events.execution_context import MethodExecutionContext
from orca.plugins.declared_tracking_observer import DeclaredTrackingObserver
from orca.resource_models.labware import LabwareInstance
from orca.state.records import (
    AspirateDetails,
    DeclaredTracking,
    DeclaredVolumeTransfer,
    DeviceOperation,
    OperationRecord,
    TrackingRecord,
    TrackingSource,
)
from orca.resource_models.tracking_interpreter import DefaultInterpreter


def test_operation_record_defaults_affected_labware_ids_empty() -> None:
    rec = OperationRecord(
        operation=DeviceOperation.SHAKE,
        device_name="shaker",
        affected_labware=["plate-27ff"],
        action_id="a1",
        thread_id="t1",
        details=AspirateDetails(labware="plate-27ff", positions=["A1"], volumes=[1.0]),
        timestamp=time.time(),
    )
    assert rec.affected_labware_ids == []


def test_default_interpreter_threads_affected_labware_ids() -> None:
    recs = DefaultInterpreter().interpret(
        command="shake",
        args=(),
        kwargs={},
        result=None,
        device_name="shaker",
        affected_labware=["plate-27ff"],
        affected_labware_ids=["27ff968a-dead-beef-0000-000000000000"],
        action_id="a1",
        thread_id="t1",
    )
    assert len(recs) == 1
    rec = recs[0]
    assert rec is not None
    assert rec.affected_labware == ["plate-27ff"]
    assert rec.affected_labware_ids == ["27ff968a-dead-beef-0000-000000000000"]


def test_declared_observer_surfaces_instance_id() -> None:
    instance = LabwareInstance("ngs_working_plate", "96_well")
    observer = DeclaredTrackingObserver()
    ctx = MethodExecutionContext(
        execution_id="wf-1",
        workflow_name="wf",
        method_id="m1",
        method_name="transfer",
    )
    declares = DeclaredTracking(
        volume_transferred=[
            DeclaredVolumeTransfer(
                source="ngs_working_plate",
                target="ngs_working_plate",
                source_wells=["A1"],
                target_wells=["B1"],
                volume_ul=10.0,
            )
        ]
    )
    record = observer.process_operations(
        operations=[],
        execution_context=ctx,
        action_id="a1",
        thread_id="t1",
        declares=declares,
        template_to_instance={"ngs_working_plate": instance},
    )
    assert record is not None
    aspirate = next(op for op in record.operations if op.operation == DeviceOperation.ASPIRATE)
    assert aspirate.affected_labware == [instance.name]
    assert aspirate.affected_labware_ids == [instance.id]


def test_affected_ids_aggregates_distinct_uuids_for_cli_render() -> None:
    rec = TrackingRecord(
        execution_id="exec-1",
        action_id="a1",
        thread_id="t1",
        method_id="m1",
        source=TrackingSource.OBSERVED,
        timestamp=time.time(),
        operations=[
            OperationRecord(
                operation=DeviceOperation.ASPIRATE,
                device_name="lh",
                affected_labware=["plate-27ff"],
                affected_labware_ids=["27ff968a-1111"],
                action_id="a1",
                thread_id="t1",
                details=AspirateDetails(labware="plate-27ff", positions=["A1"], volumes=[1.0]),
                timestamp=time.time(),
            ),
            OperationRecord(
                operation=DeviceOperation.DISPENSE,
                device_name="lh",
                affected_labware=["plate-27ff"],
                affected_labware_ids=["27ff968a-1111"],
                action_id="a1",
                thread_id="t1",
                details=AspirateDetails(labware="plate-27ff", positions=["A1"], volumes=[1.0]),
                timestamp=time.time(),
            ),
        ],
    )
    assert _affected_ids(rec) == "27ff968a-1111"


def test_operation_record_round_trips_affected_labware_ids() -> None:
    rec = OperationRecord(
        operation=DeviceOperation.ASPIRATE,
        device_name="lh",
        affected_labware=["plate-27ff"],
        affected_labware_ids=["27ff968a-1111"],
        action_id="a1",
        thread_id="t1",
        details=AspirateDetails(labware="plate-27ff", positions=["A1"], volumes=[1.0]),
        timestamp=1.0,
    )
    rebuilt = OperationRecord.model_validate_json(rec.model_dump_json())
    assert rebuilt.affected_labware_ids == ["27ff968a-1111"]
