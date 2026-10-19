"""An audited action done outside its facade writes the row the facade would."""

import dataclasses
import logging

import pytest

from orca.runtime.danger import (
    DangerLevel,
    clear_audit_trail,
    dangerous,
    list_audit_entries,
    record_audit,
)


@dangerous(
    name="test.outside_facade.delete",
    level=DangerLevel.CRITICAL,
    message="delete {position_id} on {device_id}",
)
def _delete(device_id: str, position_id: str, *, reason: str | None = None) -> None:
    del reason


def test_the_row_and_the_log_line_match_a_decorated_call(caplog: pytest.LogCaptureFixture) -> None:
    clear_audit_trail()

    with caplog.at_level(logging.INFO, logger="orca.audit"):
        _delete("pf400_1", "nowhere", reason="bad name", confirm=True)
        record_audit(
            "test.outside_facade.delete", "bad name",
            {"device_id": "pf400_1", "position_id": "nowhere"},
        )

    decorated, by_hand = list_audit_entries()
    assert dataclasses.replace(by_hand, timestamp=0) == dataclasses.replace(decorated, timestamp=0)
    [decorated_log, by_hand_log] = [r.args for r in caplog.records if r.name == "orca.audit"]
    assert by_hand_log == decorated_log


def test_the_level_comes_from_the_registered_action() -> None:
    clear_audit_trail()

    record_audit("test.outside_facade.delete", None, {"device_id": "d"})

    [entry] = list_audit_entries()
    assert entry.danger_level is DangerLevel.CRITICAL


def test_an_unregistered_action_is_refused() -> None:
    with pytest.raises(KeyError):
        record_audit("no.such.action", None, {})


def test_reason_and_confirm_never_land_in_the_args() -> None:
    clear_audit_trail()

    record_audit(
        "test.outside_facade.delete", "r",
        {"device_id": "d", "reason": "r", "confirm": True},
    )

    [entry] = list_audit_entries()
    assert entry.call_args == {"device_id": "d"}
