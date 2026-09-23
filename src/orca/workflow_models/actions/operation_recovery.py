"""Operation-level device-call recovery seam.

A device call inside an ``@orca.action`` body that fails consults a per-thread
handler (seeded on the ContextVar at thread start). The operator can pick an
OPERATION-level decision (``RETRY_OP``: re-run ONLY this call while the action
body stays suspended at its ``await``) or an ACTION-level decision (``RETRY`` /
``ABORT_*``: unwind and re-run / abort the whole action, the existing path). An
action-level decision propagates as an ``OperationDecisionSignal`` so the thread
applies it without a second pause. With no handler seeded (unit tests / no
runtime) the failure propagates unchanged -- the pre-existing action-level path.
"""
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from orca.workflow_models.status_enums import RecoveryDecision

DeviceOpRecoveryHandler = Callable[[Exception, str], Awaitable[RecoveryDecision]]

device_op_recovery_handler: ContextVar[DeviceOpRecoveryHandler | None] = ContextVar(
    "device_op_recovery_handler", default=None,
)


class OperationDecisionSignal(Exception):
    """Carries an operator's ACTION-level decision (RETRY / CONTINUE / ABORT_*) out
    of the dispatcher after an operation-level pause, so ``_handle_action_error``
    applies it without pausing again. RETRY_OP never travels this path -- it
    resolves inside the dispatcher (the op re-runs, the body stays suspended)."""

    def __init__(self, decision: RecoveryDecision, original_error: Exception) -> None:
        super().__init__(f"device op recovery -> action-level {decision.name}")
        self.decision = decision
        self.original_error = original_error
