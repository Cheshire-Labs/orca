"""Runs the last server process left running are marked failed when the next one starts.

A run's in-flight state dies with its process, so it cannot be resumed. Each
marked run also sends a run-lost event to every sink, so the event log and
live subscribers see it.
"""

import logging
import time
from collections.abc import Sequence
from datetime import datetime, timezone

from orca.events.execution_context import ExecutionLifecycleContext
from orca.events.runtime_event import RuntimeEvent
from orca.runtime.execution_record_service import IExecutionRecordService
from orca.events.emitter import IEventSink

_logger = logging.getLogger(__name__)

# The stored status is FAILED; this event status tells a lost run from one that failed on its own.
INTERRUPTED_EVENT_STATUS = "FAILED_INTERRUPTED"

INTERRUPTED_REASON = (
    "the server restarted while this execution was running; "
    "its in-flight state was not recoverable"
)


async def mark_interrupted_executions(
    records: IExecutionRecordService, sinks: Sequence[IEventSink],
) -> list[str]:
    """Mark every run left non-terminal as failed, send each sink a run-lost event, and return the ids."""
    rows = await records.list_non_terminal()
    now = datetime.now(timezone.utc)
    for row in rows:
        await records.mark_interrupted(row.execution_id, INTERRUPTED_REASON, now)
        event = RuntimeEvent(
            event_name=f"EXECUTION.{row.execution_id}.{INTERRUPTED_EVENT_STATUS}",
            execution_id=row.execution_id,
            timestamp=time.time(),
            entity_type="EXECUTION",
            entity_id=row.execution_id,
            status=INTERRUPTED_EVENT_STATUS,
            context=ExecutionLifecycleContext(
                execution_id=row.execution_id,
                workflow_name=row.workflow_name,
                reason=INTERRUPTED_REASON,
            ),
        )
        for sink in sinks:
            try:
                sink.on_event(event)
            except Exception:
                _logger.exception("the run-lost event for %s did not reach %s", row.execution_id, type(sink).__name__)
    if rows:
        _logger.info("%d run(s) left running by the last process marked failed", len(rows))
    return [row.execution_id for row in rows]
