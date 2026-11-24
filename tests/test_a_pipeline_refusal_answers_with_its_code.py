"""An operator's command the pipeline refuses, or whose channel fails, answers with a code, not a server error."""

from orca.gateway.controller.command_pipeline import PartialChannelFailureError
from orca.operations.error_mapping import known_operation_error
from orca.resource_models.device_error import TipsNotPresentError


def test_a_tip_pick_the_record_calls_empty_answers_tips_not_present() -> None:
    known = known_operation_error(TipsNotPresentError("tips_1", "id-1", ["A2"], 1))

    assert known is not None
    assert (known.surface_code(), known.http_status) == ("tips_not_present", 409)
    assert known.extras == {"labware_id": "id-1", "positions": ["A2"]}


def test_a_failed_channel_answers_channel_failed() -> None:
    known = known_operation_error(PartialChannelFailureError([]))

    assert known is not None
    assert (known.surface_code(), known.http_status) == ("channel_failed", 502)
