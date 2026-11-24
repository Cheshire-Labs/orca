"""Operator-facing actions as first-class classes.

Each Operation owns:
- a Pydantic Request shape (wire-validated input)
- a Pydantic Response shape (wire-validated output)
- a `run(req)` method that performs the action and raises `OperationError`

One Operation owns an action's request shape, validation, error mapping and
orchestration. `orca.api.mount_api` serves it as a REST route.
"""

from orca.operations.error_codes import ErrorCode, WireErrorCode
from orca.operations.protocol import OperationError
from orca.operations.scope import (
    ExecutionScope,
    Scope,
    ThreadScope,
)

__all__ = [
    "ErrorCode",
    "ExecutionScope",
    "OperationError",
    "Scope",
    "ThreadScope",
    "WireErrorCode",
]
