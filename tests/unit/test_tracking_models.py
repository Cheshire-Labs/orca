from typing import Callable

import pytest
from pydantic import ValidationError

from orca.resource_models.well_selector import WellSelector, all_wells, quadrant
from orca.state.records import (
    AspirateDetails,
    DeviceOperation,
    OperationRecord,
    ShakeDetails,
    DeclaredTracking,
    DeclaredVolumeTransfer,
)


class TestWellSelector:
    def test_quadrant_invalid(self) -> None:
        with pytest.raises(ValueError, match="Invalid quadrant"):
            quadrant("center")


def _make_well_selector() -> WellSelector:
    return all_wells()


def _make_operation_record() -> OperationRecord:
    return OperationRecord(
        operation=DeviceOperation.SHAKE,
        device_name="shaker_1",
        affected_labware=["plate_1"],
        action_id="a1",
        thread_id="t1",
        details=ShakeDetails(speed_rpm=300.0, duration_s=60.0),
        timestamp=0.0,
    )


# `WellSelector` is a frozen dataclass (raises AttributeError on mutation);
# `OperationRecord` is a Pydantic BaseModel with frozen=True (raises
# ValidationError). Both pin the immutability invariant, so they parametrize
# together against their respective expected exception types.
@pytest.mark.parametrize(
    ("factory", "field_name", "new_value", "expected_exc"),
    [
        (_make_well_selector, "mode", "quadrant", AttributeError),
        (_make_operation_record, "device_name", "other", ValidationError),
    ],
)
def test_model_is_frozen(
    factory: Callable[[], object],
    field_name: str,
    new_value: str,
    expected_exc: type[BaseException],
) -> None:
    instance = factory()
    with pytest.raises(expected_exc):
        setattr(instance, field_name, new_value)


class TestDeclaredTracking:
    def test_none_wells_resolves_to_empty_positions(self) -> None:
        """The black-box transfer (source_wells/target_wells=None) is consumed
        by DeclaredTrackingObserver as positions=[]: a recorded aspirate and
        dispense pair with empty positions/volumes, not a fan-out over wells.
        """
        from orca.events.execution_context import MethodExecutionContext
        from orca.plugins.declared_tracking_observer import DeclaredTrackingObserver
        from orca.state.records import AspirateDetails, DispenseDetails

        declares = DeclaredTracking(
            volume_transferred=[
                DeclaredVolumeTransfer(source="a", target="b", volume_ul=10.0),
            ],
        )
        context = MethodExecutionContext(
            execution_id="e1", workflow_name="wf", method_id="m1",
            method_name="transfer",
        )
        record = DeclaredTrackingObserver().process_operations(
            operations=[], execution_context=context,
            action_id="act1", thread_id="t1", declares=declares,
        )

        assert record is not None
        aspirate = next(
            op.details for op in record.operations
            if isinstance(op.details, AspirateDetails)
        )
        dispense = next(
            op.details for op in record.operations
            if isinstance(op.details, DispenseDetails)
        )
        assert aspirate.positions == []
        assert aspirate.volumes == []
        assert dispense.positions == []
        assert dispense.volumes == []
