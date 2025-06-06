import asyncio
import uuid
from typing import List

from orca.resource_models.labware import AnyLabwareTemplate, LabwareTemplate
from orca.resource_models.location import Location
from orca.sdk.events.event_bus_interface import IEventBus
from orca.sdk.events.execution_context import ExecutionContext, MethodExecutionContext, WorkflowExecutionContext
from orca.workflow_models.actions.dynamic_resource_action import DynamicResourceActionResolver, UnresolvedLocationAction
from orca.workflow_models.actions.location_action import LocationAction
from orca.workflow_models.interfaces import ILabwareThread, IMethod
from orca.workflow_models.status_enums import ActionStatus, MethodStatus
from orca.workflow_models.status_manager import StatusManager


class MethodInstance(IMethod):
    def __init__(self, name: str) -> None:
        self._id = str(uuid.uuid4())
        self._name = name
        self._actions: List[UnresolvedLocationAction] = []

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self._name

    @property
    def actions(self) -> List[UnresolvedLocationAction]:
        return self._actions

    def append_action(self, action: UnresolvedLocationAction) -> None:
        self._actions.append(action)

    def assign_thread(self, input_template: LabwareTemplate, thread: ILabwareThread) -> None:
        for step in self.actions:
            if input_template in step.expected_input_templates:
                step.assign_input(input_template, thread.labware)
            elif any(isinstance(template, AnyLabwareTemplate) for template in step.expected_input_templates):
                step.assign_input(input_template, thread.labware)


class ExecutingMethod(IMethod):
    def __init__(self, method: MethodInstance, event_bus: IEventBus, status_manager: StatusManager, context: WorkflowExecutionContext) -> None:
        self._event_bus = event_bus
        self._status_manager = status_manager
        self._context = context
        self._method = method
        self._pending_actions: List[UnresolvedLocationAction] = method.actions
        self._current_action: LocationAction | None = None
        self._completed_actions: List[LocationAction] = []
        self.status = MethodStatus.CREATED
        self._resolving_action_lock = asyncio.Lock()
        self._action_ready_event = asyncio.Event()

    @property
    def id(self) -> str:
        return self._method.id

    @property
    def name(self) -> str:
        return self._method.name

    @property
    def actions(self) -> List[UnresolvedLocationAction]:
        return self._method.actions

    def append_action(self, action: UnresolvedLocationAction) -> None:
        self._method.append_action(action)

    def assign_thread(self, input_template: LabwareTemplate, thread: ILabwareThread) -> None:
        self._method.assign_thread(input_template, thread)

    @property
    def pending_actions(self) -> List[UnresolvedLocationAction]:
        return self._pending_actions

    @property
    def completed_actions(self) -> List[LocationAction]:
        return self._completed_actions

    @property
    def current_action(self) -> LocationAction | None:
        return self._current_action

    def has_completed(self) -> bool:
        return len(self._pending_actions) == 0 and MethodStatus.COMPLETED == self.status

    @property
    def status(self) -> MethodStatus:
        status = self._status_manager.get_status(self._method.id)
        return MethodStatus[status]

    @status.setter
    def status(self, status: MethodStatus) -> None:
        context = MethodExecutionContext(self._context.workflow_id,
                                        self._context.workflow_name,
                                        self._method.id,
                                        self._method.name)
        self._status_manager.set_status("METHOD",
                                        self._method.id,
                                        status.name,
                                        context)

    def _handle_action_completed(self, event: str, context: ExecutionContext) -> None:
        assert self._current_action is not None, "Current action should not be None when handling action completion."
        self._event_bus.unsubscribe(event, self._handle_action_completed)
        self._current_action.release_reservation()  # TODO: Ensure this is the correct spot to release the reservation -- this was previously in labware thread
        self._completed_actions.append(self._current_action)
        self._current_action = None

        if len(self.pending_actions) == 0:
            self.status = MethodStatus.COMPLETED

    async def resolve_next_action(self, current_location: Location, action_resolver: DynamicResourceActionResolver) -> LocationAction | None:
        self.status = MethodStatus.IN_PROGRESS
        if len(self.pending_actions) == 0:
            self.status = MethodStatus.COMPLETED
            return None

        async with self._resolving_action_lock:
            if self._current_action is None:
                self._action_ready_event.clear()
                current_dynamic_action = self.pending_actions.pop(0)
                self._current_action = await action_resolver.resolve_action(current_dynamic_action, current_location)
                self._event_bus.subscribe(f"ACTION.{self._current_action.id}.{ActionStatus.COMPLETED.name}", self._handle_action_completed)
                self._action_ready_event.set()

        await self._action_ready_event.wait()
        return self._current_action
    
