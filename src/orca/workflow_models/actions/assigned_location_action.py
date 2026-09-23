from orca.events.event_channel import EventChannelRegistry
from orca.events.execution_context import MethodExecutionContext
from orca.resource_models.tracking_context import TrackingContext
from orca.variables.variable_store import IVariableResolver
from orca.workflow_models.actions.executable_location_action import ExecutableLocationAction
from orca.workflow_models.actions.location_action import ActionBodyLocationAction
from orca.workflow_models.status_manager import StatusManager


class AssignedLocationAction:
    """Frozen lifecycle stage between Unresolved and Executable.

    Input labware is bound (the manager is frozen onto the underlying action);
    no execution context yet. Produced exactly once per
    ``UnresolvedLocationAction`` via ``.assign()`` (idempotent). ``.executable``
    mints a FRESH ``ExecutableLocationAction`` per call -- the retry seam: one
    assigned, N executables across N execution attempts.
    """

    def __init__(self, location_action: ActionBodyLocationAction) -> None:
        self._location_action = location_action

    @property
    def location_action(self) -> ActionBodyLocationAction:
        return self._location_action

    def executable(
        self,
        status_manager: StatusManager,
        context: MethodExecutionContext,
        variable_store: IVariableResolver,
        event_channel_registry: EventChannelRegistry | None,
        tracking_context: TrackingContext | None,
        pool_indices: dict[str, int] | None = None,
        submission_id: str | None = None,
    ) -> ExecutableLocationAction:
        return ExecutableLocationAction(
            status_manager,
            self._location_action,
            context,
            variable_store,
            event_channel_registry,
            tracking_context,
            pool_indices,
            submission_id,
        )
