"""``IThreadContext`` ABC: the narrow surface that ``IMethodTemplate.schedule``
implementations receive in lieu of an ``ExecutingLabwareThread`` reference.

Double-dispatch on ``template.schedule(ctx, registry)`` replaces what was an
isinstance cascade in the yield adapter. Templates code
against this interface; they cannot downcast to the concrete thread.
``ExecutingLabwareThread`` implements ``IThreadContext`` directly.

Factory operations that need to introspect concrete template types live
in ``method_factory_helpers.py`` (free functions) so this interface
does not depend on ``ActionTemplate`` / ``MethodTemplate`` and the
templates depend only on this interface.
"""
import asyncio
from abc import ABC, abstractmethod
from typing import Callable, Optional, Protocol

from orca.events.custom_emit import EventEmitter
from orca.events.execution_context import WorkflowExecutionContext
from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.resource_models.location import Location
from orca.variables.variable_store import IVariableResolver
from orca.workflow_models.interfaces import IMethod
from orca.workflow_models.labware_threads.thread_state_machine import ThreadEvent
from orca.workflow_models.method import ExecutingMethod


_RegisterMethodTemplate = Callable[[object], None]


class IMySlotView(Protocol):
    """Narrow view of the thread's LabwareSlot exposed to template
    ``schedule()`` bodies via ``ctx.my_slot()``.

    The concrete ``LabwareSlot`` (``resource_models/labware_state.py``)
    satisfies this Protocol structurally. Templates code against this
    surface so they cannot reach the slot's queue internals directly.
    """

    @property
    def is_closed(self) -> bool: ...

    def queue_empty(self) -> bool: ...

    async def await_next_method(
        self, stop_event: asyncio.Event,
    ) -> ExecutingMethod | None:
        """Wait for the next queued ``ExecutingMethod`` or a terminal condition.

        Returns ``None`` only if the slot is closed AND drained, or if
        ``stop_event`` fires before a method arrives. Otherwise returns the
        dequeued method. The close sentinel is a wakeup, not a verdict: it is
        discarded, and work queued behind it is still served.
        """
        ...


class IThreadContext(ABC):
    """The thread-internal surface exposed to ``IMethodTemplate.schedule``.

    Keeps templates from reaching into ``ExecutingLabwareThread``
    internals. Flag at review if it crosses ~19 members.
    """

    @property
    @abstractmethod
    def thread_id(self) -> str: ...

    @property
    @abstractmethod
    def labware(self) -> LabwareInstance: ...

    @property
    @abstractmethod
    def labware_template(self) -> LabwareTemplate | None: ...

    @property
    @abstractmethod
    def current_location(self) -> Location: ...

    @property
    @abstractmethod
    def stop_event(self) -> asyncio.Event: ...

    @property
    @abstractmethod
    def shared_executing_method(self) -> ExecutingMethod | None: ...

    @property
    @abstractmethod
    def variable_store(self) -> IVariableResolver: ...

    @property
    @abstractmethod
    def submission_id(self) -> str | None:
        """The submission this thread was started by, if any.

        Variables resolve submission-first, so anything building a context off
        this thread has to carry it or its reads fall back to the workflow
        default without saying so.
        """
        ...

    @property
    @abstractmethod
    def execution_context(self) -> WorkflowExecutionContext: ...

    @property
    @abstractmethod
    def event_emitter(self) -> EventEmitter | None:
        """Workflow event-bus emitter, so contexts built off this thread can
        surface their ctx.emit publishes as CUSTOM runtime events."""
        ...

    @property
    @abstractmethod
    def register_method_template(self) -> Optional[_RegisterMethodTemplate]: ...

    @abstractmethod
    def my_slot_key(self) -> str: ...

    @abstractmethod
    def bind_method(self, method: IMethod) -> None:
        """Assign this thread's labware to ``method`` so the method
        knows which labware to act on. No-op if the thread has no
        labware template (e.g. pre-T6 paths).
        """
        ...

    @abstractmethod
    def my_slot(self) -> IMySlotView | None:
        """Return the thread's labware slot, or ``None`` if no slot has
        been provisioned (pre-T6 paths).
        """
        ...

    @abstractmethod
    def mark_my_labware_parked(self) -> None:
        """Transition this thread's labware to ``PARKED`` in the registry."""
        ...

    @abstractmethod
    def add_method(self, method: IMethod) -> None:
        """Register ``method`` in the runtime's method registry so the
        engine can look it up later by id.
        """
        ...

    @abstractmethod
    def create_executing_method(self, method: IMethod) -> ExecutingMethod:
        """Create the live ``ExecutingMethod`` record for ``method``
        against this thread's execution context.
        """
        ...

    @abstractmethod
    def release_holdover(self) -> None:
        """Release any reservation held over from the previous action.

        Called by templates that cross a method boundary (``JoinTemplate``
        before waiting on the next method, ``ParkTemplate`` before the
        park move) so the previous method's reservation does not block
        peers.
        """
        ...

    @abstractmethod
    def location(self, name: str) -> Location:
        """Resolve a taught name to a ROUTING node, the way `start=`/`end=` do.

        A bare device name gives its site, never the off-graph reservation
        mutex -- routing to the mutex finds no path at all.
        """
        ...

    @abstractmethod
    async def fire_and_execute_move_to(
        self, event: ThreadEvent, target: Location | list[Location],
        abandon_when: Callable[[], bool] | None = None,
    ) -> None:
        """Fire ``event``, resolve a MoveAction from this thread's
        current location to ``target`` (avoiding the previous location
        for backtrack suppression), then execute it. The thread id,
        labware, source location, and previous-location lookup are
        supplied internally.

        Wraps the four-step move pipeline (fire / read previous /
        resolve / execute) into one operation. Templates call this in
        a loop until ``ctx.current_location`` equals ``target``.

        ``abandon_when`` marks the move ABANDONABLE: the predicate is checked
        on each rejected reservation cycle (true -> ``MoveAbandonedError``,
        no reservation held). Starvation never abandons a move; a starved
        move keeps waiting at rest.
        """
        ...
