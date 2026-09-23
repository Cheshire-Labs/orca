"""IEventSink that mirrors execution lifecycle into the execution-record store.

Listens for ``SUBMISSION.{id}.ACCEPTED``, ``THREAD.{id}.{status}`` and
``EXECUTION.{id}.{terminal}`` and forwards them to the
``ExecutionRecordService``. A thin adapter: it parses the event context and
calls the Service's sync write methods; the queue, drain, and DB work all live
in the Service.
"""

from datetime import datetime, timezone

from orca.events.execution_context import (
    ExecutionLifecycleContext,
    SubmissionExecutionContext,
    ThreadExecutionContext,
)
from orca.events.runtime_event import RuntimeEvent
from orca.runtime.execution_record import ExecutionState
from orca.runtime.execution_record_service import ExecutionRecordService
from orca.runtime.interfaces import IEventSink


class ExecutionTrackingSink(IEventSink):
    """``IEventSink`` that drives the ``ExecutionRecordService`` from events."""

    def __init__(self, service: ExecutionRecordService) -> None:
        self._service = service

    def on_event(self, event: RuntimeEvent) -> None:
        if event.entity_type == "SUBMISSION" and event.status == "ACCEPTED":
            ctx = event.context
            if not isinstance(ctx, SubmissionExecutionContext):
                return
            self._service.record_running(
                execution_id=ctx.execution_id,
                workflow_name=ctx.workflow_name,
                submitted_at=datetime.fromtimestamp(event.timestamp, tz=timezone.utc),
            )
            return
        if event.entity_type == "THREAD":
            ctx = event.context
            if not isinstance(ctx, ThreadExecutionContext):
                return
            # Every transition upserts, so the record always holds the thread's
            # last-known state and a restart can still say what the run did.
            self._service.record_thread(
                ctx.execution_id,
                thread_id=ctx.thread_id,
                name=ctx.thread_name,
                template_name=ctx.template_name,
                status=event.status,
                last_error=ctx.last_error,
                pause_reason=ctx.pause_reason,
            )
            return
        if event.entity_type == "EXECUTION":
            state = ExecutionState.__members__.get(event.status)
            if state is None or not state.is_terminal():
                return
            ctx = event.context
            reason: str | None = None
            if isinstance(ctx, ExecutionLifecycleContext):
                reason = ctx.reason
            self._service.mark_terminal(
                execution_id=event.execution_id,
                state=state,
                reason=reason,
                terminal_at=datetime.fromtimestamp(event.timestamp, tz=timezone.utc),
            )
