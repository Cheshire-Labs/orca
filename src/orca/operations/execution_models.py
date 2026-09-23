"""Wire models for the execution operations.

Apart from `execution.py` because the Operation classes there take
`ISystemRuntime`, and the CLI reads these models over HTTP without
ever wanting the engine.
"""

import dataclasses
from typing import Literal
from typing_extensions import Self
from pydantic import BaseModel, ConfigDict
from orca.daemon.schemas import ThreadSnapshotDTO
from orca.runtime.execution_phase import ExecutionPhase
from orca.runtime.execution_record import ExecutionRecord
from orca.runtime.status_models import ExecutionDetail, ExecutionStatus, ThreadSnapshot


class CloseExecutionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str


class CloseExecutionResponse(BaseModel):
    """Returned by close_execution; mirrors `SubmissionCloseResult`."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str
    phase: str


class GetExecutionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str


class ExecutionRecordModel(BaseModel):
    """Wire shape for a single execution record.

    ``paused`` is separate from ``status`` on purpose: the pause latch is
    orthogonal to the phase, so a stopped run still reports `accepting` or
    `draining`. A surface that reads only the phase shows a run the operator
    stopped as still running, which is what "stop did nothing" looked like.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    workflow_name: str
    status: str
    error: str | None = None
    paused: bool = False
    pause_reason: str | None = None
    """Who set the latch: `manual` for an operator's stop, `system` when the
    runtime paused itself (a stall, an unresolvable deadlock). A recoverable
    timeout pauses the threads without the latch, so it reads not-paused
    here and shows on the thread statuses instead."""
    abort_armed: bool = False
    """Whether a second confirmed stop would abort. Set by the first stop
    call, cleared by resume."""

    @classmethod
    def from_record(cls, rec: ExecutionRecord) -> Self:
        return cls(
            id=rec.id,
            workflow_name=rec.workflow_name,
            status=rec.status.value if hasattr(rec.status, "value") else str(rec.status),
            error=rec.error,
            paused=rec.paused,
            pause_reason=rec.pause_reason,
            abort_armed=rec.abort_armed,
        )

    @classmethod
    def from_status(cls, status: ExecutionStatus) -> Self:
        """Build from `ExecutionStatus` (rich-phase shape).

        Callers use `runtime.get_execution_status` which returns the
        rich `ExecutionPhase` vocabulary; we surface that as the wire
        `status` so summary and detail endpoints align (Bug FFF).
        `ExecutionPhase.value` is the lowercase wire string
        (`"accepting"` etc.); `str(phase)` would give
        `"ExecutionPhase.ACCEPTING"` on Python 3.10 which is wrong on
        the wire.
        """
        return cls(
            id=status.id,
            workflow_name=status.workflow_name,
            status=status.status.value,
            error=status.error,
            paused=status.paused,
            pause_reason=status.pause_reason,
            abort_armed=status.abort_armed,
        )


class GetExecutionResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution: ExecutionRecordModel


class ListExecutionsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ListExecutionsResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    executions: list[ExecutionRecordModel]


class GetExecutionDetailRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str


class GetExecutionDetailResponse(BaseModel):
    """Mirrors `ExecutionDetail`. Thread snapshots are surfaced as dicts.
    Counters mirror the dataclass and let polling clients show
    progress without walking the full thread list.
    """
    model_config = ConfigDict(extra="allow")

    id: str
    workflow_name: str
    status: str
    error: str | None = None
    threads: list[ThreadSnapshotDTO] = []
    total_thread_count: int = 0
    completed_thread_count: int = 0
    active_thread_count: int = 0
    paused: bool = False
    pause_reason: str | None = None
    abort_armed: bool = False

    @classmethod
    def from_detail(cls, detail: ExecutionDetail) -> Self:
        # ``detail.status`` may be an ``ExecutionPhase`` enum (when produced
        # by the live runtime, despite the dataclass's ``str`` annotation)
        # or a raw string from the sink-rehydrate path (already mapped
        # through ``ExecutionState.value``). isinstance narrows to the enum
        # so the ``.value`` access is type-safe.
        status_raw = detail.status
        status_value = (
            status_raw.value
            if isinstance(status_raw, ExecutionPhase)
            else str(status_raw)
        )
        return cls(
            id=detail.id,
            workflow_name=detail.workflow_name,
            status=status_value,
            error=detail.error,
            threads=[_thread_snapshot_to_dto(t) for t in detail.threads],
            total_thread_count=detail.total_thread_count,
            completed_thread_count=detail.completed_thread_count,
            active_thread_count=detail.active_thread_count,
            paused=detail.paused,
            pause_reason=detail.pause_reason,
            abort_armed=detail.abort_armed,
        )


class StopExecutionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str
    confirm: bool = False


class StopExecutionResponse(BaseModel):
    """Returned by stop_execution -- a two-call confirmed abort.

    ``status`` is ``"armed"`` after the first call (or any call that finds the
    execution not-yet-armed): the execution is paused immediately and abort is
    armed, but NOT aborted -- re-call with ``confirm=true`` to abort, or resume
    to disarm. ``status`` is ``"aborted"`` once a confirmed call on an armed,
    still-paused execution runs the abort.

    ``status`` is ``"already_terminal"`` if the execution had already finished
    (COMPLETED/FAILED/ABORTED) when the stop arrived -- nothing was armed or
    aborted, the call is a no-op.

    ``phase`` carries the ExecutionPhase after the request: a non-terminal
    value (the execution is paused but still ACCEPTING/DRAINING) when armed, the
    accurate terminal ``"aborted"`` when aborted, or the pre-existing terminal
    value for ``already_terminal``. The runtime drains the done-callback before
    returning, so ``phase`` never reports a stale ``"stopping"`` on the abort
    path. Operator surfaces render the message from ``status``.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["armed", "aborted", "already_terminal"]
    execution_id: str
    phase: str
    message: str


class RemoveExecutionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str


class RemoveExecutionResponse(BaseModel):
    """Returned by remove_execution. Status is the literal ``"removed"``."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["removed"] = "removed"
    execution_id: str


def _thread_snapshot_to_dto(snap: ThreadSnapshot) -> ThreadSnapshotDTO:
    """Convert a ThreadSnapshot dataclass to its wire DTO mirror.

    ``ThreadSnapshotDTO`` (orca.daemon.schemas) mirrors ThreadSnapshot
    field-for-field; ``dataclasses.asdict`` recursively flattens
    nested dataclasses (``current_method: MethodSnapshot | None``) so
    ``model_validate`` constructs the full typed DTO without per-field
    extraction.
    """
    return ThreadSnapshotDTO.model_validate(dataclasses.asdict(snap))
