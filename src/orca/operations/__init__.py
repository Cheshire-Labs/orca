"""Operator-facing actions as first-class classes.

Each Operation owns:
- a Pydantic Request shape (wire-validated input)
- a Pydantic Response shape (wire-validated output)
- a `run(req)` method that performs the action and raises `OperationError`

Surface binders (the orca-core daemon REST plus a hosted deployment's
REST and MCP surfaces) reshape Operations onto wire protocols. One
Operation = one binding per surface. One Operation owns an action's
request shape, validation, error mapping and orchestration; the binders
wire that one object onto REST, MCP and the daemon's own REST.
"""

from orca.operations._protocol import (
    Operation,
    OperationError,
    OperationErrorCode,
)
from orca.operations._scope import (
    ExecutionScope,
    Scope,
    ThreadScope,
)

__all__ = [
    "ExecutionScope",
    "Operation",
    "OperationError",
    "OperationErrorCode",
    "Scope",
    "ThreadScope",
]
