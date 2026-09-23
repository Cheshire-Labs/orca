"""Tests for the `Scope` discriminated union value object."""

import pytest
from pydantic import TypeAdapter, ValidationError

from orca.operations._scope import (
    ExecutionScope,
    Scope,
    ThreadScope,
)

_SCOPE_ADAPTER: TypeAdapter[ExecutionScope | ThreadScope] = TypeAdapter(Scope)


def test_execution_scope_kind_defaulted() -> None:
    scope = ExecutionScope(execution_id="e1")
    assert scope.kind == "execution"
    assert scope.execution_id == "e1"


def test_thread_scope_kind_defaulted() -> None:
    scope = ThreadScope(execution_id="e1", thread_id="t1")
    assert scope.kind == "thread"


def test_scope_is_frozen() -> None:
    scope = ExecutionScope(execution_id="e1")
    with pytest.raises(ValidationError):
        scope.execution_id = "e2"


def test_scope_rejects_extras() -> None:
    with pytest.raises(ValidationError):
        ExecutionScope(execution_id="e1", extra="nope")  # type: ignore[call-arg]


def test_discriminator_routes_execution() -> None:
    parsed = _SCOPE_ADAPTER.validate_python(
        {"kind": "execution", "execution_id": "e1"},
    )
    assert isinstance(parsed, ExecutionScope)


def test_discriminator_routes_thread() -> None:
    parsed = _SCOPE_ADAPTER.validate_python(
        {"kind": "thread", "execution_id": "e1", "thread_id": "t1"},
    )
    assert isinstance(parsed, ThreadScope)


def test_discriminator_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        _SCOPE_ADAPTER.validate_python(
            {"kind": "system", "execution_id": "e1"},
        )
