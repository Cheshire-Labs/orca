"""Wire models for the thread operations.

Apart from `thread.py` because the Operation classes there take
`ISystemRuntime`, and the CLI reads these models over HTTP without
ever wanting the engine.
"""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import Self
from orca.operations._scope import Scope
from orca.runtime.status_models import ThreadSnapshot
from orca.workflow_models.status_enums import RecoveryDecision


class PauseRequest(BaseModel):
    """Pause an execution (all threads) or a single thread.

    `scope` discriminates: ExecutionScope pauses every thread in the
    execution; ThreadScope pauses one. `reason` is optional and is
    stamped onto the audit trail when set.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    scope: Scope
    reason: str | None = None


class _PauseExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["execution"] = "execution"
    pausing: int
    already_paused: int
    terminal_skipped: int


class _PauseThreadResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["thread"] = "thread"
    execution_id: str
    thread_id: str
    status: Literal["pausing"] = "pausing"


PauseResult = _PauseExecutionResult | _PauseThreadResult


class PauseResponse(BaseModel):
    """Wire response. `result` is either `_PauseExecutionResult` (counts)
    or `_PauseThreadResult` (single-thread acknowledgement).
    """
    model_config = ConfigDict(frozen=True, extra="forbid")
    result: PauseResult = Field(..., discriminator="kind")


class ResumeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    scope: Scope
    reason: str | None = None


class _ResumeExecutionResult(BaseModel):
    """Mirrors ``SystemRuntime.resume_all_threads`` 4-counter contract.

    See orca.runtime.system_runtime.SystemRuntime.resume_all_threads for
    semantics; each counter is non-overlapping and the four sum to the
    total threads scoped to the execution.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["execution"] = "execution"
    resumed: int
    pause_cancelled: int
    error_skipped: int
    completed_skipped: int


class _ResumeThreadResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["thread"] = "thread"
    execution_id: str
    thread_id: str
    status: Literal["resumed"] = "resumed"


ResumeResult = _ResumeExecutionResult | _ResumeThreadResult


class ResumeResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    result: ResumeResult = Field(..., discriminator="kind")


class SpawnThreadRequest(BaseModel):
    """Spawn a new thread from a template into a running execution.

    Primary use case is AUTO_SPAWN_FAILED incident recovery.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: str
    template_name: str = Field(
        ...,
        description=(
            "Thread template name as registered on the runtime "
            "(GET /catalog/threads on the daemon or "
            "GET /api/workflows on a hosted deployment for entry-thread templates)."
        ),
    )
    labware_id: str | None = Field(
        default=None,
        description=(
            "Optional: bind the spawned thread to a specific labware "
            "instance by id. When omitted, the runtime uses the "
            "template's default acquisition."
        ),
    )


class _ActionSnapshotProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    command: str
    status: str
    position_id: str
    resource_name: str
    description: str | None = None


class _MethodSnapshotProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    name: str
    status: str
    current_action: _ActionSnapshotProjection | None
    completed_action_count: int


class SpawnThreadResponse(BaseModel):
    """Full projection of the spawned `ThreadSnapshot`.

    Mirrors `orca.daemon.schemas.ThreadSnapshotDTO`. Callers depend on
    the full snapshot to show the freshly-spawned thread's location,
    current method, and completed-method context without a follow-up
    round-trip. `id` is the thread id; status / current_location /
    pause_reason / etc. come straight off the runtime snapshot.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    status: str
    current_location: str
    current_method: _MethodSnapshotProjection | None
    completed_method_count: int
    last_error: str | None
    pause_reason: str | None
    completed_methods: tuple[str, ...]
    labware_template_name: str | None = None
    paused_device_command: str | None = None
    pause_message: str | None = None
    pause_site: str | None = None
    """Mirror of ``ThreadSnapshot.pause_site``: WHERE the thread stopped."""
    honoured_decisions: tuple[str, ...] = ()
    """The recovery decisions this thread will accept right now, and the only
    ones to offer. Empty when it is not error-paused. Anything else is refused
    and the thread stays paused."""
    waiting_for: str | None = None

    @classmethod
    def from_snapshot(cls, snap: ThreadSnapshot) -> Self:
        current_method: _MethodSnapshotProjection | None = None
        if snap.current_method is not None:
            m = snap.current_method
            current_action: _ActionSnapshotProjection | None = None
            if m.current_action is not None:
                a = m.current_action
                current_action = _ActionSnapshotProjection(
                    id=a.id,
                    command=a.command,
                    status=str(a.status),
                    position_id=a.position_id,
                    resource_name=a.resource_name,
                    description=a.description,
                )
            current_method = _MethodSnapshotProjection(
                id=m.id,
                name=m.name,
                status=str(m.status),
                current_action=current_action,
                completed_action_count=m.completed_action_count,
            )
        return cls(
            id=snap.id,
            name=snap.name,
            status=str(snap.status),
            current_location=snap.current_location,
            current_method=current_method,
            completed_method_count=snap.completed_method_count,
            last_error=snap.last_error,
            pause_reason=snap.pause_reason,
            completed_methods=snap.completed_methods,
            labware_template_name=snap.labware_template_name,
            paused_device_command=snap.paused_device_command,
            pause_message=snap.pause_message,
            pause_site=snap.pause_site,
            honoured_decisions=snap.honoured_decisions,
            waiting_for=snap.waiting_for,
        )


class RecoverThreadRequest(BaseModel):
    """Apply a RecoveryDecision to an error-paused thread.

    `decision` is one of RETRY / RETRY_OP / CONTINUE / ABORT_ACTION /
    ABORT_METHOD / ABORT_THREAD (case-insensitive on the wire). RETRY_OP re-runs
    only the failed device call and is valid only while a device op is paused; on
    a device that supports it the engine re-reads hardware state before
    re-issuing, and refuses the retry when only a human can resolve what it
    finds. CONTINUE says the work is done and the run may carry on: valid while
    an action is error-paused, and at a failed move once the labware's recorded
    location is the move's target, which is the operator saying they carried it
    there. It needs something to call finished and a known position, so it is
    refused at a resolution or spawn pause.

    RETRY, RETRY_OP and CONTINUE all say the instrument has been looked at and
    is fit to drive, so each clears the device fault this pause is about before
    the thread resumes. Recovering is enough on its own; a separate
    clear-device-fault is not needed and the first RETRY is not refused by the
    fault the operator has just dealt with. The three aborts say nothing about
    the machine and leave the fault standing, which is right: giving up on the
    work is exactly when a plate can still be in the jaws.

    Manually-paused threads use Resume instead; this endpoint targets
    threads in error pause (`pause_reason="error"`).

    No `reason` field: the underlying `runtime.threads.recover()` does
    not accept a reason kwarg (the `@dangerous` decorator on it does
    NOT have `requires_reason=True`). Earlier drafts threaded `reason`
    through and crashed with `TypeError: got an unexpected keyword
    argument 'reason'` at runtime; the MagicMock-driven tests didn't
    catch it because `AsyncMock` accepts arbitrary kwargs. If audit-trail
    reason is needed, the facade has to grow the parameter first
    (or a separate audit-log helper has to record it out-of-band,
    matching the pre-Phase-3 thread_mutation handler).
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: str
    thread_id: str
    decision: str


class RecoverThreadResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["recovered"] = "recovered"
    execution_id: str
    thread_id: str


class SkipMethodRequest(BaseModel):
    """Skip a pending method on a paused thread."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str
    thread_id: str
    method_id: str | None = None
    method_name: str | None = None
    reason: str


class _ThreadMutationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["mutated"] = "mutated"
    execution_id: str
    thread_id: str


class AbortMethodRequest(BaseModel):
    """Abort the in-progress method on a paused thread.

    Targets the currently-assigned IN_PROGRESS method (the one method
    that has started and cannot simply be skipped). For pending
    methods use SkipMethod instead.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str
    thread_id: str
    method_id: str | None = None
    method_name: str | None = None
    reason: str


class SkipActionRequest(BaseModel):
    """Skip a pending action on a paused thread."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str
    thread_id: str
    action_id: str | None = None
    action_command: str | None = None
    reason: str


InsertWhere = Literal["head", "tail", "before", "after"]


class InsertMethodRequest(BaseModel):
    """Insert a method into a paused thread's lane.

    Provide EITHER `template_name` (look up an existing method
    template by name) OR `method_code` (compile a one-method module
    from source). The code path is validated by
    `compile_method_code` before exec; `orca` and `topology` are
    pre-bound, so a method body may declare device-targeting actions via
    `topology.device(name, Iface)` exactly as a workflow file does.

    `where` is head/tail/before/after; `anchor` is the existing method
    name to position relative to (required for before/after, forbidden
    for head/tail).
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: str
    thread_id: str
    template_name: str | None = None
    method_code: str | None = None
    where: InsertWhere
    anchor: str | None = None
    reason: str


class ReplaceResult(BaseModel):
    """Result of a replace_method / replace_action.

    `staged_for_recovery` is True when the target was the thread's
    in-progress/errored step: the replacement is staged to run next but the
    failed step is NOT dropped automatically. `recommended_decision` is the
    `RecoveryDecision` to pass to recover_thread next (serializes to the same
    string a client would send back); `next_step` is the same guidance as
    human/LLM prose. Both are None when the target was pending and fully
    spliced.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: str
    thread_id: str
    staged_for_recovery: bool = False
    recommended_decision: RecoveryDecision | None = None
    next_step: str | None = None


class ReplaceMethodRequest(BaseModel):
    """Replace a method with a substitute.

    Provide EITHER `template_name` or `method_code` for the replacement
    (same rules as insert -- `method_code` is exec'd with `orca` + a live
    `topology` pre-bound, so it may declare device-targeting actions via
    `topology.device(name, Iface)`). `target_name` is the method to replace, matched by
    name first-match (Before anchors + skip are one-shot on first consumption;
    method names are not unique on the lane, so the first occurrence is the one
    replaced). A PENDING target is spliced in place. The current error-paused
    method is staged to run next; the response carries `staged_for_recovery=true`
    with `recommended_decision="ABORT_METHOD"` and a `next_step`. Replacing the
    current method on a manually-paused thread is refused.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: str
    thread_id: str
    target_name: str
    template_name: str | None = None
    method_code: str | None = None
    reason: str


class InsertActionRequest(BaseModel):
    """Insert an action into the IN_PROGRESS method's action lane.

    `action_code` MUST define exactly one `@orca.action` decorated
    callable, written exactly as in a workflow file. `orca` and
    `topology` are pre-bound in the exec namespace, so the action targets
    a device the same way build_workflow does:
    `@orca.action(device=topology.device("mlstar_1", LiquidHandler),
    inputs=[AnyLabwareTemplate()])`. `topology.device(name, Iface)` /
    `topology.pool(name)` resolve the LIVE running devices, so the
    injected action reserves and executes identically to a workflow
    action. The daemon validates the source before exec via
    `compile_action_code`. No template-name lookup -- the engine
    registry doesn't expose action templates by name today.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: str
    thread_id: str
    action_code: str
    where: InsertWhere
    anchor: str | None = None
    reason: str


class ReplaceActionRequest(BaseModel):
    """Replace an action with a substitute.

    `action_code` MUST define exactly one `@orca.action` callable (the
    replacement), written as in a workflow file -- `orca` + a live
    `topology` are pre-bound, so it targets devices via
    `topology.device(name, Iface)` (same as insert_action).
    `target_command` is the action command to replace. The
    replacement always runs next (AtHead), ahead of any other pending actions
    in the method; the skip is one-shot on the first pending consumption of
    `target_command`. A PENDING target is spliced (replacement runs next,
    original skipped). The current error-paused action is staged; the response
    carries `staged_for_recovery=true` with
    `recommended_decision="ABORT_ACTION"` and a `next_step`. Replacing the
    current action on a manually-paused thread is refused.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: str
    thread_id: str
    target_command: str
    action_code: str
    reason: str
