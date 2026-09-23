"""ExecutionRecord and ExecutionState live in their own module so they can
be imported by both `system_runtime.py` and `runtime_interface.py` without
forming a cycle.

`system_runtime.py` defines the runtime behavior and transitively imports
the sub-facades, which in turn import `runtime_interface.py` for the ABC
contracts. Moving the data types here breaks the cycle: both ends now import
from this leaf module.
"""

from dataclasses import dataclass
from enum import Enum


class ExecutionState(Enum):
    """Task-level state of one submitted workflow execution.

    Not to be confused with T6's `ExecutionStatus` enum (submission lifecycle:
    CREATED/ACCEPTING/DRAINING/...). This one tracks the asyncio task that
    runs the workflow -- from submission through terminal outcome.
    """
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"

    def is_terminal(self) -> bool:
        """True when the execution has reached a final outcome.

        Terminal states are safe for cleanup operations like
        `remove_execution`. Explicit allow-list: adding a new non-terminal
        state (e.g. PAUSED, PENDING) should not accidentally be classified
        as terminal by default.
        """
        return self in (
            ExecutionState.COMPLETED,
            ExecutionState.FAILED,
            ExecutionState.ABORTED,
        )


@dataclass
class ExecutionRecord:
    """Snapshot of an execution's current state (mutable; updated by the runtime).

    Canonical identity is `id`. CLI / REST / MCP clients always key on it.
    """
    id: str
    workflow_name: str
    status: ExecutionState
    error: str | None = None
    paused: bool = False
    """Whether the execution-level pause latch is set. Separate from
    ``status``, which a pause never changes. False on a record rehydrated
    from a terminal DB row: a finished run holds no latch."""
    pause_reason: str | None = None
    """Who set the latch: `manual` for an operator's stop, `system` when the
    runtime paused itself."""
    abort_armed: bool = False
    """Whether a second confirmed stop would abort."""
