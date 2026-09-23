"""ThreadContext: the ``ctx`` object passed to @orca.thread generator functions.

Provides event coordination (wait_for/emit) and variable access for yield threads.
No device() -- device actions happen inside yielded methods, not at the thread level.

Event read modes:
- ctx.wait_for() uses consumed semantics: each call advances past the last-seen publish,
  so calling ctx.wait_for("result") in a loop gets each NEW publish, not the same one.
- This differs from static orca.on() which uses latch semantics (seen_counter=0).
"""

import asyncio
from typing import Callable

from pydantic import JsonValue

from orca.events.custom_emit import EventEmitter, emit_custom_event
from orca.events.event_channel import EventChannelRegistry
from orca.resource_models.labware import LabwareInstance
from orca.variables.errors import OptionValue
from orca.variables.variable_store import IVariableResolver

PartnerConstraintSetter = Callable[[str, dict[str, str]], None]
HasMoreWorkFn = Callable[[], bool]


class ThreadContext:
    """Execution context for @orca.thread generator functions.

    Provides:
    - ``ctx.wait_for(name, timeout)`` -- Wait for a named event (consumed semantics)
    - ``ctx.emit(name, value, data)`` -- Publish event to other threads
    - ``ctx.param(name)`` -- Variable resolution
    - ``ctx.has_more_work()`` -- (T6e) True while the thread's slot may still
      receive contributions; replaces hardcoded ``for i in range(N)`` loops
      around ``orca.join(...)`` with submission-driven termination.
    """

    def __init__(
        self,
        event_channel_registry: EventChannelRegistry,
        variable_store: IVariableResolver,
        execution_id: str,
        partner_constraint_setter: PartnerConstraintSetter | None = None,
        labware: LabwareInstance | None = None,
        has_more_work_fn: HasMoreWorkFn | None = None,
        submission_id: str | None = None,
        event_emitter: EventEmitter | None = None,
        workflow_name: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        self._event_channel_registry = event_channel_registry
        self._variable_store = variable_store
        self._execution_id = execution_id
        self._submission_id = submission_id
        self._seen_counters: dict[str, int] = {}
        self._partner_constraint_setter = partner_constraint_setter
        self._labware = labware
        self._has_more_work_fn = has_more_work_fn
        self._event_emitter = event_emitter
        self._workflow_name = workflow_name
        self._thread_id = thread_id

    def has_more_work(self) -> bool:
        """True if this thread's receiver slot can still get more contributions.

        Receivers use this in ``while ctx.has_more_work(): yield orca.join(...)``
        patterns. Four gates, in order: queued items -> True (even if closed);
        else closed -> False; else spent receiver -> False, because waiting for
        work this labware cannot serve strands whatever overflowed past it;
        else ``slot.has_room()`` -- so an OPEN slot at max_contributions returns
        False, finalizing a full receiver rather than looping for a
        contribution the policy would overflow to a fresh one.

        The slot closes when its contribution window is over: either no thread
        that transitively feeds this receiver up the ``contributes_to`` chain is
        still live (not just its direct feeders), or, for a receiver with no
        declared feeder, no in-scope worker thread remains live
        (ExecutingWorkflow._evaluate_slot_closures). A thread waiting for an
        operator to collect its labware counts as finished for both: its work
        is done and only the physical pickup is outstanding.

        A no-feeder slot closes on worker quiescence alone. Nothing waits to
        establish that a later submission will not feed it -- an unarrived
        submission holds no slot open, and only an injection already in flight
        defers the close (ExecutingWorkflow.injecting).

        A thread with no receiver slot always gets True: the caller is expected
        to exit via a different mechanism (e.g. a fixed ``for i in range(N)``
        loop).
        """
        if self._has_more_work_fn is None:
            return True
        return self._has_more_work_fn()

    @property
    def labware(self) -> LabwareInstance:
        if self._labware is None:
            raise RuntimeError("Labware not available in this context")
        return self._labware

    async def wait_for(
        self, event_name: str, timeout: float | None = None
    ) -> tuple[str | None, dict[str, JsonValue]]:
        """Wait for a named event. Returns (value, data).

        Uses consumed semantics: each call advances past the last-seen
        publish counter, so calling wait_for() in a loop sees each new publish.
        """
        channel = self._event_channel_registry.get_or_create(event_name)
        seen = self._seen_counters.get(event_name, 0)
        counter, value, data = await channel.wait(seen_counter=seen, timeout=timeout)
        self._seen_counters[event_name] = counter
        return value, data

    async def emit(
        self, event_name: str, value: str | None = None, data: dict[str, JsonValue] | None = None
    ) -> None:
        """Publish an event visible to all threads in this workflow, and to
        the runtime event surface as CUSTOM.<name>.EMITTED."""
        channel = self._event_channel_registry.get_or_create(event_name)
        await channel.publish(value=value, data=data)
        emit_custom_event(
            self._event_emitter,
            execution_id=self._execution_id,
            workflow_name=self._workflow_name,
            thread_id=self._thread_id,
            event_name=event_name,
            value=value,
            data=data,
        )

    async def param(self, name: str) -> OptionValue:
        """Resolve a variable by name from the execution's variable store.

        If this thread is tagged with a submission_id, the per-submission
        partition is consulted first before the execution-wide partition.
        """
        return self._variable_store.resolve(
            name, self._execution_id, submission_id=self._submission_id,
        )

    def set_partner_constraint(self, template_name: str, constraints: dict[str, str]) -> None:
        """Set constraints for partner selection during auto-spawn.

        When this thread's action triggers auto-spawn for a labware matching
        template_name, the constraints are passed to the labware finder to
        select the correct partner (e.g., by barcode).
        """
        if self._partner_constraint_setter is None:
            raise RuntimeError(
                "Partner constraints not available in this context"
            )
        self._partner_constraint_setter(template_name, constraints)
