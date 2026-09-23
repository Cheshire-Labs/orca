"""Tests for the `Operation` Protocol + `OperationError` family."""

import pytest

from orca.operations._protocol import (
    Operation,
    OperationError,
    OperationErrorCode,
)
from orca.operations.system import GetSystemInfoOperation
from orca.runtime.facades.registry import IRegistryFacade


class _RuntimeWithRegistry:
    """Minimal runtime exposing the `registry` the Operation ctor needs.

    The structural Protocol check never invokes `run`, so the registry
    is never dereferenced; the property exists only to satisfy the
    Operation's compositional ctor."""

    @property
    def registry(self) -> IRegistryFacade:
        raise NotImplementedError


def test_real_operation_satisfies_operation_protocol() -> None:
    """A shipping Operation (`GetSystemInfoOperation`) satisfies the
    Protocol structurally without inheriting from it. Binding the
    structural check to production code means a Protocol shape change
    (a renamed `run`/`Request`/`Response` member) breaks here. The
    reference Operation's `run()` behavior and error path live in
    `tests/operations/test_system_info.py`.
    """
    op = GetSystemInfoOperation(runtime=_RuntimeWithRegistry())
    assert isinstance(op, Operation)


def test_operation_error_factories_set_code() -> None:
    err = OperationError.not_found("missing X")
    assert err.code == OperationErrorCode.NOT_FOUND
    assert err.message == "missing X"
    assert err.extras is None


def test_operation_error_factory_captures_extras() -> None:
    err = OperationError.conflict("busy", execution_id="e1")
    assert err.code == OperationErrorCode.CONFLICT
    assert err.extras == {"execution_id": "e1"}


def test_operation_error_is_raiseable() -> None:
    """OperationError must be a real exception so `raise` works."""
    with pytest.raises(OperationError) as excinfo:
        raise OperationError.invalid_input("bad shape")
    assert excinfo.value.code == OperationErrorCode.INVALID_INPUT
    assert str(excinfo.value) == "bad shape"


def test_all_error_codes_have_factories() -> None:
    """Every code in the enum should have a corresponding factory method."""
    factories = {
        OperationErrorCode.NOT_FOUND: OperationError.not_found,
        OperationErrorCode.INVALID_INPUT: OperationError.invalid_input,
        OperationErrorCode.CONFLICT: OperationError.conflict,
        OperationErrorCode.SERVICE_UNAVAILABLE: OperationError.service_unavailable,
        OperationErrorCode.UNAUTHORIZED: OperationError.unauthorized,
    }
    for code, factory in factories.items():
        err = factory("test")
        assert err.code == code
