"""An audited action done outside its facade writes the row the facade would."""

import logging

import pytest

from orca.runtime.danger import (
    DangerLevel,
    action_descriptor,
    dangerous,
    record_audit,
)

from tests.audit_capture import AuditCapture


@dangerous(
    name="test.outside_facade.delete",
    level=DangerLevel.CRITICAL,
    message="delete {position_id} on {device_id}",
)
def _delete(device_id: str, position_id: str, *, reason: str | None = None) -> None:
    del reason


def test_the_row_and_the_log_line_match_a_decorated_call(
    audit: AuditCapture, caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="orca.audit"):
        _delete("pf400_1", "nowhere", reason="bad name", confirm=True)
        record_audit(
            action_descriptor(_delete), "bad name",
            {"device_id": "pf400_1", "position_id": "nowhere"},
        )

    decorated, by_hand = audit.rows()
    assert by_hand.model_copy(update={"timestamp": 0}) == decorated.model_copy(update={"timestamp": 0})
    [decorated_log, by_hand_log] = [r.args for r in caplog.records if r.name == "orca.audit"]
    assert by_hand_log == decorated_log


def test_the_level_comes_from_the_registered_action(audit: AuditCapture) -> None:
    record_audit(action_descriptor(_delete), None, {"device_id": "d"})

    [entry] = audit.rows()
    assert entry.danger_level == DangerLevel.CRITICAL.name


def test_reason_and_confirm_never_land_in_the_args(audit: AuditCapture) -> None:
    record_audit(
        action_descriptor(_delete), "r",
        {"device_id": "d", "reason": "r", "confirm": True},
    )

    [entry] = audit.rows()
    assert entry.call_args == {"device_id": "d"}
