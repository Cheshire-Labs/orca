"""Tests for the `OperationError` family."""

import re
from enum import unique

import pytest

from orca.operations.error_codes import ErrorCode, WireErrorCode
from orca.operations.protocol import OperationError


def test_an_error_is_sent_with_the_status_its_code_carries() -> None:
    err = OperationError.of(ErrorCode.LABWARE_NOT_FOUND, "missing X")
    assert (err.surface_code(), err.http_status) == ("labware_not_found", 404)
    assert err.message == "missing X"
    assert err.extras is None


def test_operation_error_factory_captures_extras() -> None:
    err = OperationError.of(ErrorCode.CONFLICT, "busy", execution_id="e1")
    assert err.http_status == 409
    assert err.extras == {"execution_id": "e1"}


def test_operation_error_is_raiseable() -> None:
    """OperationError must be a real exception so `raise` works."""
    with pytest.raises(OperationError) as excinfo:
        raise OperationError.validation_error("bad shape")
    assert (excinfo.value.surface_code(), excinfo.value.http_status) == ("validation_error", 422)
    assert str(excinfo.value) == "bad shape"


def test_every_code_is_one_lower_case_snake_case_spelling() -> None:
    spellings = [str(code) for code in ErrorCode]
    assert [s for s in spellings if not re.fullmatch(r"[a-z]+(_[a-z]+)*", s)] == []


def test_a_code_spelled_twice_in_one_enum_is_refused_when_the_enum_is_defined() -> None:
    with pytest.raises(ValueError, match="duplicate values"):
        @unique
        class _Twice(WireErrorCode):
            FIRST = ("same_code", 404)
            SECOND = ("same_code", 409)


class _HostedCode(WireErrorCode):
    ONLY_HOSTED = ("only_hosted", 418)


def test_a_code_from_another_enum_on_the_same_base_is_sent_with_its_own_status() -> None:
    err = OperationError.of(_HostedCode.ONLY_HOSTED, "hosted only")
    assert (err.surface_code(), err.http_status) == ("only_hosted", 418)
