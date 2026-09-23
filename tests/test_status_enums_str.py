"""Characterization tests for the (str, Enum) conversion of the five
status enums in ``orca.workflow_models.status_enums``.

Pins both axes of the conversion:

- ``.value`` is now a string identical to ``.name`` (G1b: was opaque
  ``auto()`` int; now intrinsic string).
- Existing in-process semantics keep working: equality against the enum
  member, ``EnumClass[name]`` reverse lookup, ``.name`` attribute,
  ``str(member)`` (now ``"EnumClass.NAME"`` is replaced by just the
  value string -- because ``str`` is the mixed-in primary base).

The conversion is wire-shape-stable for every site that previously
emitted ``.name`` to JSON: ``json.dumps(<enum>)`` now emits the same
canonical name string because ``str`` is a base.

Mirrors the G1 conversion already shipped for ``FailurePolicy``
(commit ``2173d45`` on ``cleanup/g1-failure-policy-str-enum``). G1
covered DTO round-trips because ``FailurePolicy`` is the only one of
these enums that actually appears on a Pydantic DTO field
(``MethodTemplateDTO.failure_policy``). The five enums in this G1b
batch are not DTO field types in either repo, so the contract is
tested at the enum surface directly.
"""

import json

import pytest

from orca.workflow_models.status_enums import (
    ActionStatus,
    LabwareThreadStatus,
    MethodStatus,
    RecoveryDecision,
    WorkflowStatus,
)


# -- .value == .name for every member ---------------------------------------


def test_action_status_value_equals_name() -> None:
    for member in ActionStatus:
        assert member.value == member.name


def test_method_status_value_equals_name() -> None:
    for member in MethodStatus:
        assert member.value == member.name


def test_labware_thread_status_value_equals_name() -> None:
    for member in LabwareThreadStatus:
        assert member.value == member.name


def test_workflow_status_value_equals_name() -> None:
    for member in WorkflowStatus:
        assert member.value == member.name


def test_recovery_decision_value_equals_name() -> None:
    for member in RecoveryDecision:
        assert member.value == member.name


# -- str base mixed in: members are str instances ---------------------------


def test_action_status_members_are_str() -> None:
    assert isinstance(ActionStatus.COMPLETED, str)


def test_method_status_members_are_str() -> None:
    assert isinstance(MethodStatus.IN_PROGRESS, str)


def test_labware_thread_status_members_are_str() -> None:
    assert isinstance(LabwareThreadStatus.PAUSED, str)


def test_workflow_status_members_are_str() -> None:
    assert isinstance(WorkflowStatus.COMPLETED, str)


def test_recovery_decision_members_are_str() -> None:
    assert isinstance(RecoveryDecision.RETRY, str)


# -- json.dumps emits the canonical name string -----------------------------


def test_action_status_json_dumps_to_name() -> None:
    assert json.dumps(ActionStatus.COMPLETED) == '"COMPLETED"'
    assert json.dumps(ActionStatus.AWAITING_CO_THREADS) == '"AWAITING_CO_THREADS"'


def test_method_status_json_dumps_to_name() -> None:
    assert json.dumps(MethodStatus.IN_PROGRESS) == '"IN_PROGRESS"'
    assert json.dumps(MethodStatus.PARTIAL_COMPLETE) == '"PARTIAL_COMPLETE"'


def test_labware_thread_status_json_dumps_to_name() -> None:
    assert json.dumps(LabwareThreadStatus.PAUSED) == '"PAUSED"'
    assert (
        json.dumps(LabwareThreadStatus.AWAITING_MOVE_TARGET_AVAILABILITY)
        == '"AWAITING_MOVE_TARGET_AVAILABILITY"'
    )


def test_workflow_status_json_dumps_to_name() -> None:
    assert json.dumps(WorkflowStatus.COMPLETED) == '"COMPLETED"'
    assert json.dumps(WorkflowStatus.ERRORED) == '"ERRORED"'


def test_recovery_decision_json_dumps_to_name() -> None:
    assert json.dumps(RecoveryDecision.RETRY) == '"RETRY"'
    assert json.dumps(RecoveryDecision.ABORT_THREAD) == '"ABORT_THREAD"'


# -- reverse lookup via EnumClass[name] still works -------------------------


def test_action_status_reverse_lookup_by_name() -> None:
    member = ActionStatus["COMPLETED"]
    assert member is ActionStatus.COMPLETED
    # The reverse-lookup result must carry the str-mixin shape, not an
    # opaque auto() value: it is a str equal to its own name.
    assert isinstance(member, str)
    assert member == "COMPLETED"
    assert ActionStatus["AWAITING_CO_THREADS"] == "AWAITING_CO_THREADS"


def test_method_status_reverse_lookup_by_name() -> None:
    member = MethodStatus["IN_PROGRESS"]
    assert member is MethodStatus.IN_PROGRESS
    assert isinstance(member, str)
    assert member == "IN_PROGRESS"


def test_labware_thread_status_reverse_lookup_by_name() -> None:
    member = LabwareThreadStatus["PAUSED"]
    assert member is LabwareThreadStatus.PAUSED
    assert isinstance(member, str)
    assert member == "PAUSED"


def test_workflow_status_reverse_lookup_by_name() -> None:
    member = WorkflowStatus["COMPLETED"]
    assert member is WorkflowStatus.COMPLETED
    # The reverse-lookup result must carry the str-mixin shape, not an
    # opaque auto() value: it is a str equal to its own name.
    assert isinstance(member, str)
    assert member == "COMPLETED"


def test_recovery_decision_reverse_lookup_by_name() -> None:
    member = RecoveryDecision["RETRY"]
    assert member is RecoveryDecision.RETRY
    # The reverse-lookup result must carry the str-mixin shape, not an
    # opaque auto() value: it is a str equal to its own name.
    assert isinstance(member, str)
    assert member == "RETRY"


def test_reverse_lookup_unknown_name_raises() -> None:
    with pytest.raises(KeyError):
        ActionStatus["NOPE"]
    with pytest.raises(KeyError):
        RecoveryDecision["nope"]  # case-sensitive


# -- string-equality against the value string -------------------------------


def test_action_status_equals_name_string() -> None:
    # str-Enum members compare equal to their string value.
    assert ActionStatus.COMPLETED == "COMPLETED"


def test_recovery_decision_equals_name_string() -> None:
    assert RecoveryDecision.RETRY == "RETRY"


# -- value lookup via EnumClass(value) --------------------------------------


def test_action_status_value_lookup() -> None:
    assert ActionStatus("COMPLETED") is ActionStatus.COMPLETED


def test_recovery_decision_value_lookup() -> None:
    assert RecoveryDecision("RETRY") is RecoveryDecision.RETRY
    assert RecoveryDecision("ABORT_THREAD") is RecoveryDecision.ABORT_THREAD


# -- identity / equality across the enum surface ----------------------------


def test_enum_identity_preserved() -> None:
    # The mixed-in str base does not break enum identity.
    assert ActionStatus.COMPLETED is ActionStatus.COMPLETED
    assert MethodStatus.IN_PROGRESS is MethodStatus.IN_PROGRESS
    assert LabwareThreadStatus.PAUSED is LabwareThreadStatus.PAUSED
    assert WorkflowStatus.COMPLETED is WorkflowStatus.COMPLETED
    assert RecoveryDecision.RETRY is RecoveryDecision.RETRY


def test_member_count_unchanged() -> None:
    # Lock the inventory so a future drop-or-rename surfaces here.
    # LabwareThreadStatus 14 -> 16: added
    # AWAITING_MANUAL_PLACE + AWAITING_MANUAL_REMOVE for the LIVE-mode
    # operator wait paths in ManualPlaceSpawn / ManualRemoveSpawn.
    # LabwareThreadStatus 16 -> 17: added
    # AWAITING_ACTION_RESERVATION so the dashboard surfaces the
    # reservation-retry wait with candidate locations populated on
    # ``waiting_for`` instead of leaving the thread silently parked
    # in ``RESOLVING_ACTION_LOCATION``.
    # LabwareThreadStatus 17 -> 18: added crash-only terminal FAILED (an
    # unhandled thread error lands terminal instead of freezing non-terminal).
    assert len(ActionStatus) == 13
    assert len(MethodStatus) == 5
    assert len(LabwareThreadStatus) == 18
    assert len(WorkflowStatus) == 4
    # RecoveryDecision 4 -> 5: added RETRY_OP (operation-level device retry;
    # re-run only the failed device call, action body stays suspended).
    # 5 -> 6: added CONTINUE (carry on to the next action after one errored,
    # recorded as operator-confirmed rather than executed).
    assert len(RecoveryDecision) == 6
