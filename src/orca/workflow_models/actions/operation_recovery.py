"""Operation-level device-call recovery seam.

A device call inside an ``@orca.action`` body that fails, or a wait in it that runs out of time,
consults a per-thread handler (seeded on the ContextVar at thread start). The operator can pick an
OPERATION-level decision (``RETRY``: re-run ONLY this call while the action
body stays suspended at its ``await``) or an ACTION-level decision (``CONTINUE`` /
``ABORT_*``: unwind and carry on past / abort the whole action). An
action-level decision propagates as an ``OperationDecisionSignal`` so the thread
applies it without a second pause. With no handler seeded (unit tests / no
runtime) the failure propagates unchanged -- the pre-existing action-level path.
"""
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass

from orca.workflow_models.status_enums import PauseSite, RecoveryDecision

DeviceOpRecoveryHandler = Callable[[Exception, str, PauseSite], Awaitable[RecoveryDecision]]

device_op_recovery_handler: ContextVar[DeviceOpRecoveryHandler | None] = ContextVar(
    "device_op_recovery_handler", default=None,
)


@dataclass(frozen=True)
class UnsavedResult:
    """A command that ran and the device's answer to it, as JSON, which the record does not have."""

    command: str
    request: str
    result: str


class OperationDecisionSignal(Exception):
    """Carries an operator's ACTION-level decision (CONTINUE / ABORT_*) out
    of the dispatcher after an operation-level pause, so ``_handle_action_error``
    applies it without pausing again. RETRY never travels this path -- it
    resolves inside the dispatcher (the op re-runs, the body stays suspended)."""

    def __init__(
        self, decision: RecoveryDecision, original_error: Exception, unsaved: UnsavedResult | None = None,
    ) -> None:
        super().__init__(f"device op recovery -> action-level {decision.name}")
        self.decision = decision
        self.original_error = original_error
        self.unsaved = unsaved
