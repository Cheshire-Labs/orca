"""Guards for GetLabwareJourney wire DTOs."""

from orca.operations.labware_models import JourneyAction
from orca.state.records import ShakeDetails


def test_journey_action_accepts_none_method_id() -> None:
    """JourneyAction.method_id mirrors TrackingRecord.method_id (str | None).

    Bootstrap initial-state seeds and free-floating actions belong to no
    enclosing method, so the record's method_id is None. A required-str DTO
    raised a pydantic ValidationError and 500'd POST /get-labware-journey for
    any labware whose journey included such an action (e.g. the start-location
    auto-fulfilled start-location seed).
    """
    action = JourneyAction(
        source="observed",
        timestamp=1.0,
        device_name="shaker_1",
        operation="shake",
        details=ShakeDetails(speed_rpm=200.0, duration_s=30.0),
        execution_id="exec-1",
        thread_id="thr-1",
        action_id="act-1",
        method_id=None,
    )
    assert action.method_id is None
    assert action.model_dump(mode="json")["method_id"] is None
