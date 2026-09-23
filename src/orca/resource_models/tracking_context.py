"""Execution-time tracking handles, scoped to one execution_id at write time.

The system holds a system-bound TrackingContext (sentinel ``_system``); the
runtime forks a per-execution copy as part of the workflow factory path so
each ExecutingWorkflow's records land in its own bucket. The actual write
happens through the IOpsHistoryStore that backs every OpsHistory instance.
"""
from dataclasses import dataclass

from orca.state.ops_history import OpsHistory
from orca.state.records import TrackingRecord
from orca.resource_models.tracking_observer import ITrackingObserver


@dataclass
class TrackingContext:
    """Pairs the active observer with a per-execution OpsHistory writer.

    ``ops_history`` is bound to one execution_id at construction; its
    ``store_record`` writes synchronously into that execution's bucket.
    For action-execution callers, ``store_record(execution_id, record)``
    routes to the matching bucket (forking an OpsHistory view if the bound
    execution differs from the caller's, so the same TrackingContext can
    serve every execution running in this runtime).
    """
    observer: ITrackingObserver
    ops_history: OpsHistory

    async def store_record(
        self,
        record: TrackingRecord,
        execution_id: str | None = None,
    ) -> None:
        """Write ``record`` keyed by an execution_id.

        ``execution_id`` defaults to the bound OpsHistory's execution_id
        (the system sentinel for a system-wide TrackingContext). Action
        execution call sites pass the execution_id resolved from
        ``MethodExecutionContext.execution_id`` so each record lands in the
        right per-execution bucket regardless of which TrackingContext
        instance was wired in at System construction.
        """
        target_execution_id = execution_id or self.ops_history.execution_id
        if target_execution_id == self.ops_history.execution_id:
            await self.ops_history.append_record(record)
            return
        await self.ops_history.for_execution(target_execution_id).append_record(record)
