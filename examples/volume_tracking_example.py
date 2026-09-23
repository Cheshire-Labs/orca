"""Volume tracking example: reservoir -> plate distribution.

Demonstrates the full tracking layering in isolation, with no system
runtime or workflow execution required:

    Layer 1: OpsHistory (system service)
    Layer 2: ledger_projections.well_volume (pure function)
    Layer 3: LabwareInstance.can_continue(demand) (layered predicate)

Seeds a trough (50 mL) and a plate (empty), dispenses 100 uL into 8
destination wells, and prints a per-well volume report. No hardware,
no mocks required (the action-execution write path is async, so the
example uses an asyncio entrypoint).

Run, from the repo root:
    python -m examples.volume_tracking_example
"""
import asyncio
import time

from orca.state.projections import op_count, well_volume, well_volumes
from orca.state.ops_history import OpsHistory
from orca.state.records import (
    AspirateDetails,
    DeviceOperation,
    DispenseDetails,
    InitialStateDetails,
    OperationRecord,
    TrackingRecord,
    TrackingSource,
)


# Every record names the execution it belongs to. This one stands alone.
EXECUTION_ID = "volume_tracking_example"


def _record(ops: list[OperationRecord]) -> TrackingRecord:
    return TrackingRecord(
        execution_id=EXECUTION_ID,
        action_id="distribute",
        thread_id="example_thread",
        method_id="distribute_method",
        source=TrackingSource.OBSERVED,
        timestamp=time.time(),
        operations=ops,
    )


def _op(op_type: DeviceOperation, affected: list[str], details) -> OperationRecord:
    return OperationRecord(
        operation=op_type,
        device_name="liquid_handler",
        affected_labware=affected,
        action_id="distribute",
        thread_id="example_thread",
        details=details,
        timestamp=time.time(),
    )


async def main() -> None:
    history = OpsHistory()

    # Seed: trough starts full at 50,000 uL, plate starts empty.
    trough_initial = 50000.0
    dest_wells = ["A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1"]
    per_well = 100.0

    await history.append_initial_state(
        "wash_trough",
        InitialStateDetails(labware="wash_trough", well_volumes={"A1": trough_initial}),
    )
    await history.append_initial_state(
        "sample_plate",
        InitialStateDetails(labware="sample_plate", well_volumes={w: 0.0 for w in dest_wells}),
    )

    # Distribute 100 uL into each destination well via 8-channel pipetting.
    total_volume = per_well * len(dest_wells)
    await history.append_record(_record([
        _op(
            DeviceOperation.ASPIRATE,
            ["wash_trough"],
            AspirateDetails(labware="wash_trough", positions=["A1"], volumes=[total_volume]),
        ),
        _op(
            DeviceOperation.DISPENSE,
            ["sample_plate"],
            DispenseDetails(labware="sample_plate", positions=dest_wells, volumes=[per_well] * len(dest_wells)),
        ),
    ]))

    trough_ops = await history.ops_for("wash_trough")
    plate_ops = await history.ops_for("sample_plate")

    print("Trough (wash_trough):")
    print(f"  A1 volume: {well_volume(trough_ops, 'wash_trough', 'A1'):.1f} uL")
    print(f"  op_count:  {op_count(trough_ops, 'wash_trough')}")
    print()
    print("Plate (sample_plate):")
    for well, vol in sorted(well_volumes(plate_ops, 'sample_plate').items()):
        print(f"  {well} volume: {vol:.1f} uL")
    print(f"  op_count:  {op_count(plate_ops, 'sample_plate')}")

    # Assertions: trough lost total, plate wells each received per_well.
    assert well_volume(trough_ops, "wash_trough", "A1") == trough_initial - total_volume
    for well in dest_wells:
        assert well_volume(plate_ops, "sample_plate", well) == per_well
    print("\nAll volumes match expectations.")


if __name__ == "__main__":
    asyncio.run(main())
