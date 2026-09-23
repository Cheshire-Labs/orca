from typing import Protocol

from orca.events.execution_context import MethodExecutionContext
from orca.resource_models.labware import LabwareInstance
from orca.state.records import DeclaredTracking, OperationRecord, TrackingRecord


class ITrackingObserver(Protocol):
    def process_operations(
        self,
        operations: list[OperationRecord],
        execution_context: MethodExecutionContext,
        action_id: str,
        thread_id: str,
        declares: DeclaredTracking | None = None,
        template_to_instance: dict[str, LabwareInstance] | None = None,
    ) -> TrackingRecord | None: ...


class NullTrackingObserver:
    def process_operations(
        self,
        operations: list[OperationRecord],
        execution_context: MethodExecutionContext,
        action_id: str,
        thread_id: str,
        declares: DeclaredTracking | None = None,
        template_to_instance: dict[str, LabwareInstance] | None = None,
    ) -> TrackingRecord | None:
        return None
