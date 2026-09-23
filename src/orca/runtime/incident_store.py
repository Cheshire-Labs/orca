"""Incident domain model and the per-DB store interface.

A second class of runtime errors never bubbles through an action: auto-spawn
failures, co-labware timeouts, barcode mismatches, plugin/handler exceptions,
deadlock detection, driver init failures, etc. These are recorded as
``SystemIncident``s so operators can see them via ``orca incident list``.
Recording is an ADDITIVE observation: the error is still raised/logged as today.

This module holds the domain types and ``IIncidentStore`` (the dumb per-DB
persistence interface). The orchestration -- sync off-loop ``record``, the queue
+ drain, event emission, and locking -- lives in ``IncidentService``; the SQLite
implementation is ``SqliteIncidentStore``.
"""

import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from orca.system.reservation_manager.errors import (
    ActionContinuedContext,
    ActionFailedContext,
    MoveContinuedContext,
    MoveFailedContext,
    OrphanedBacklogContext,
    ThreadDiedContext,
    UnresolvableDeadlockContext,
)


class IncidentCategory(str, Enum):
    """Classification for non-Action runtime errors.

    One incident per occurrence. Adding new categories is safe; the CLI and
    future RPC consumers treat unknown categories as OTHER.

    (str, Enum) so member.value equals member.name; the wire shape is
    intrinsic and JSON dumpers emit the string name without per-site
    `.name` stamping.
    """
    VARIABLE_RESOLUTION = "VARIABLE_RESOLUTION"
    VARIABLE_VALIDATION = "VARIABLE_VALIDATION"
    CO_LABWARE_TIMEOUT = "CO_LABWARE_TIMEOUT"
    AUTO_SPAWN_FAILED = "AUTO_SPAWN_FAILED"
    BARCODE_MISMATCH = "BARCODE_MISMATCH"
    RESERVATION_DEADLOCK = "RESERVATION_DEADLOCK"
    UNRESOLVABLE_DEADLOCK = "UNRESOLVABLE_DEADLOCK"
    RECOVERABLE_TIMEOUT = "RECOVERABLE_TIMEOUT"
    ACTION_FAILED = "ACTION_FAILED"
    ACTION_CONTINUED = "ACTION_CONTINUED"
    MOVE_FAILED = "MOVE_FAILED"
    MOVE_CONTINUED = "MOVE_CONTINUED"
    DEVICE_INIT_FAILED = "DEVICE_INIT_FAILED"
    DEVICE_BUSY_EXHAUSTED = "DEVICE_BUSY_EXHAUSTED"
    PLUGIN_HANDLER_EXCEPTION = "PLUGIN_HANDLER_EXCEPTION"
    EVENT_HANDLER_EXCEPTION = "EVENT_HANDLER_EXCEPTION"
    UNRESOLVED_ANCHOR_INSERT = "UNRESOLVED_ANCHOR_INSERT"
    SYSTEM_STALL = "SYSTEM_STALL"
    ORPHANED_BACKLOG = "ORPHANED_BACKLOG"
    THREAD_DIED = "THREAD_DIED"
    DECK_RECONCILE_CONFLICT = "DECK_RECONCILE_CONFLICT"
    LEDGER_CONTRADICTED = "LEDGER_CONTRADICTED"
    OTHER = "OTHER"


class IncidentSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class RecoveryAction(str, Enum):
    """Suggested recovery command shown after `orca incident get <id>`.

    Not enforced; the operator chooses. NONE means the incident is
    informational or structurally not recoverable in-process (restart only).
    """
    THREAD_RECOVER_RETRY = "THREAD_RECOVER_RETRY"   # fix variable, recover with retry
    # Re-run only the failed device call, hardware reconciled first. Advised
    # instead of THREAD_RECOVER_RETRY when the thread is paused inside a call.
    THREAD_RECOVER_RETRY_OP = "THREAD_RECOVER_RETRY_OP"
    THREAD_RECOVER_ABORT = "THREAD_RECOVER_ABORT"   # skip the action that triggered it
    PLUGIN_DISABLE = "PLUGIN_DISABLE"               # quarantine a misbehaving plugin
    MANUAL_SPAWN = "MANUAL_SPAWN"                   # for AUTO_SPAWN_FAILED
    RESTART_EXECUTION = "RESTART_EXECUTION"         # stop + remove + resubmit
    RESUME_EXECUTION = "RESUME_EXECUTION"           # resume accepts the partial fill
    NONE = "NONE"                                   # informational; no action possible


# -- Typed per-category detail dataclasses ------------------------------
# Replaces `dict[str, Any]` detail so mis-recording is caught at compile time.


@dataclass(frozen=True)
class VariableResolutionDetail:
    var_name: str
    action_command: str | None


@dataclass(frozen=True)
class VariableValidationDetail:
    var_name: str
    offered_value: str                  # repr; avoids carrying arbitrary values through Any
    expected_type: str
    reason: str


@dataclass(frozen=True)
class CoLabwareTimeoutDetail:
    missing_labware_names: tuple[str, ...]
    waited_seconds: float
    position_id: str


@dataclass(frozen=True)
class AutoSpawnFailedDetail:
    requested_labware_name: str
    requesting_thread_id: str


@dataclass(frozen=True)
class BarcodeMismatchDetail:
    expected_barcode: str
    scanned_barcode: str | None
    context: str                        # e.g. "join_lookup", "action_precondition"


@dataclass(frozen=True)
class DeadlockDetail:
    cycling_thread_ids: tuple[str, ...]
    yielding_thread_id: str
    reroute_applied: bool


@dataclass(frozen=True)
class SystemStallDetail:
    """Every live thread is internally blocked with none in flight: the system
    cannot progress on its own. ``waits`` holds one ``id [status] waiting on X``
    line per stalled thread so the operator sees the mutual block."""
    stalled_thread_ids: tuple[str, ...]
    waits: tuple[str, ...]


@dataclass(frozen=True)
class DeviceInitFailedDetail:
    device_name: str
    driver_error: str


@dataclass(frozen=True)
class DeviceBusyExhaustedDetail:
    device_name: str
    retry_count: int
    last_error: str


@dataclass(frozen=True)
class DeckReconcileConflictDetail:
    """One labware the ledger and a liquid handler's deck disagree about.

    Nothing here resolves itself: the record stays and the operator settles it
    with edit-labware-location (it moved) or discharge (it is gone).
    ``driver_site`` is where the DRIVER has it, in the form its own commands
    take, or None when the driver has it nowhere.
    """
    device_name: str
    labware_id: str
    labware_name: str
    position_id: str
    reason: str
    driver_site: str | None = None
    blocking_labware_name: str | None = None
    detail: str | None = None
    """What the two sides each say, in words, when the reason needs it."""


@dataclass(frozen=True)
class LedgerContradictionDetail:
    """An operator command that only makes sense if the record was wrong.

    The command is believed and folded: the operator was at the bench and the
    record was not. What it cannot supply is the rest of the labware, so the
    fold stays wrong by an amount nothing here can compute, and the labware is
    marked for a person to state. ``positions`` are the ones the record did not
    back; ``believed`` is what it held instead, in words.
    """
    device_name: str
    command: str
    labware_id: str | None
    labware_name: str
    positions: list[str]
    believed: str


@dataclass(frozen=True)
class PluginHandlerExceptionDetail:
    plugin_type_name: str
    event_name: str
    exception_type: str
    exception_message: str


@dataclass(frozen=True)
class EventHandlerExceptionDetail:
    handler_name: str
    event_name: str
    exception_type: str
    exception_message: str


@dataclass(frozen=True)
class UnresolvedAnchorInsertDetail:
    anchor_name: str
    direction: str                      # "before" | "after"
    target_type: str                    # "method" | "action"
    item_name: str                      # name of the method/action that was waiting
    # True: the anchor ran and the insert was still queued at the end.
    # False: the anchor name never came past at all.
    anchor_reached: bool


@dataclass(frozen=True)
class RecoverableTimeoutContext:
    """Detail for a RECOVERABLE_TIMEOUT incident.

    The engine's device-command dispatcher declares this when an in-flight
    device command exceeds its ``max_seconds`` without a response. The
    execution pauses; the operator decides via REST / MCP / CLI whether to
    extend the wait, abort the command, or mark it manually complete. The
    engine's recoverable-timeout coordinator holds the call until that
    decision arrives, then resumes the paused execution.
    """
    device_id: str
    command: str
    command_id: str
    elapsed_seconds: float
    max_seconds: float


@dataclass(frozen=True)
class OtherIncidentDetail:
    message_extra: str


IncidentDetail = (
    VariableResolutionDetail
    | VariableValidationDetail
    | CoLabwareTimeoutDetail
    | AutoSpawnFailedDetail
    | BarcodeMismatchDetail
    | DeadlockDetail
    | SystemStallDetail
    | UnresolvableDeadlockContext  # engine-side error context doubles as persisted detail
    | ActionFailedContext  # thread-side action-error context doubles as persisted detail
    | ThreadDiedContext  # workflow-side uncaught-crash context doubles as persisted detail
    | ActionContinuedContext  # thread-side continue-past-error context doubles as persisted detail
    | MoveFailedContext  # thread-side move-error context doubles as persisted detail
    | MoveContinuedContext  # thread-side operator-finished-move context doubles as persisted detail
    | OrphanedBacklogContext  # workflow-side quarantine context doubles as persisted detail
    | RecoverableTimeoutContext
    | DeviceInitFailedDetail
    | DeviceBusyExhaustedDetail
    | DeckReconcileConflictDetail
    | LedgerContradictionDetail
    | PluginHandlerExceptionDetail
    | EventHandlerExceptionDetail
    | UnresolvedAnchorInsertDetail
    | OtherIncidentDetail
)


@dataclass(frozen=True)
class SystemIncident:
    id: str
    timestamp: float
    category: IncidentCategory
    severity: IncidentSeverity
    execution_id: str | None
    thread_id: str | None
    message: str                        # human-readable summary, one line
    detail: IncidentDetail              # typed; matches category
    recovery_action: RecoveryAction
    acknowledged: bool


def build_incident(
    category: IncidentCategory,
    severity: IncidentSeverity,
    message: str,
    detail: IncidentDetail,
    recovery_action: RecoveryAction,
    execution_id: str | None = None,
    thread_id: str | None = None,
) -> SystemIncident:
    """Construct a SystemIncident with a fresh id and the current timestamp."""
    return SystemIncident(
        id=str(uuid.uuid4()),
        timestamp=time.time(),
        category=category,
        severity=severity,
        execution_id=execution_id,
        thread_id=thread_id,
        message=message,
        detail=detail,
        recovery_action=recovery_action,
        acknowledged=False,
    )


class IIncidentStore(Protocol):
    """Dumb per-DB persistence for incidents.

    The only DB-aware code in the incident vertical: raw async reads/writes, no
    locking, no events (those live in ``IncidentService``). orca ships
    ``SqliteIncidentStore``; a hosted deployment injects its own DB-backed store;
    both satisfy this interface and the Service is injected with whichever.
    """

    async def create_schema(self) -> None:
        """Create the incidents table if absent (in-memory/sim setup; file and
        Postgres deployments use migrations, where this is an idempotent no-op)."""
        ...

    async def insert(self, incident: SystemIncident) -> None: ...

    async def get(self, incident_id: str) -> SystemIncident | None:
        """The incident, or None if unknown (the Service maps None to KeyError)."""
        ...

    async def fetch(
        self,
        *,
        unacknowledged_only: bool = False,
        category: IncidentCategory | None = None,
        execution_id: str | None = None,
        since: float | None = None,
    ) -> list[SystemIncident]:
        """Incidents matching the filters, ordered by timestamp ascending."""
        ...

    async def mark_acked(self, incident_id: str, acknowledged_at: datetime) -> None:
        """Mark one incident acknowledged. No-op if unknown. Idempotent."""
        ...

    async def mark_all_acked(
        self, category: IncidentCategory | None, acknowledged_at: datetime
    ) -> int:
        """Acknowledge every unacknowledged incident (optionally filtered by
        category). Returns the count newly acknowledged."""
        ...

    async def aclose(self) -> None:
        """Release the store's resources (e.g. dispose the DB engine)."""
        ...
