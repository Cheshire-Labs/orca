"""ThreadFacade: per-thread control plus runtime mutation.

Delegates to `SystemRuntime`'s existing pause/resume/recover/mutate methods
and to `MutationCoordinator` for skip/insert. The facade's job is to
(a) validate arguments uniformly, (b) apply the `@dangerous` decorators for
consistent confirm gating, (c) build `ThreadSnapshot`s for read methods,
(d) serialize concurrent mutations on the same thread via a per-thread lock.

The `spawn_thread` recovery helper is used when an AUTO_SPAWN_FAILED
incident leaves the engine without a matching thread template to wake:
operator names the template explicitly and the facade composes thread
creation, registration, and start on the running `ExecutingWorkflow`.
"""

import asyncio
from typing import Callable, List

from orca.events.runtime_event import RuntimeEvent
from orca.runtime.danger import DangerLevel, dangerous
from orca.runtime.runtime_interface import (
    IThreadFacade,
    IThreadMutationContext,
    IThreadRuntimeAccess,
)
from orca.runtime.status_models import ThreadSnapshot
from orca.system.system_interface import ISystem
from orca.variables.errors import OptionValue
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.mutation_position import InsertPosition
from orca.workflow_models.status_enums import RecoveryDecision


_TERMINAL_THREAD_STATUSES: frozenset[str] = frozenset(
    {"COMPLETED", "STOPPED", "ABORTED", "FAILED"}
)

_DECISIONS_THAT_SAY_THE_DEVICE_IS_FIT: frozenset[RecoveryDecision] = frozenset({
    RecoveryDecision.RETRY,
    RecoveryDecision.RETRY_OP,
    RecoveryDecision.CONTINUE,
})


class _SystemMutationContext(IThreadMutationContext):
    """Narrow mutation context passed to `mutate_on_next_pause` callbacks.

    Wraps an ISystem so callbacks can touch variables + mutation without
    receiving the full god interface. Not itself a @dangerous surface --
    callers already passed confirmation upstream to reach this point.
    """

    def __init__(self, system: ISystem) -> None:
        self._system = system

    def set_variable(self, name: str, value: OptionValue, execution_id: str) -> None:
        self._system.variable_store.set(name, value, execution_id)

    def get_variable(self, name: str, execution_id: str) -> OptionValue:
        return self._system.variable_store.resolve(name, execution_id)

    def skip_method(
        self, thread_id: str, *,
        method_id: str | None = None, method_name: str | None = None,
    ) -> None:
        self._system.skip_pending_method(thread_id, method_id, method_name)

    def insert_method(
        self, thread_id: str, template: MethodTemplate,
        where: InsertPosition,
    ) -> None:
        self._system.insert_method(thread_id, template, where)

    def skip_action(
        self, thread_id: str, *,
        action_id: str | None = None, action_command: str | None = None,
    ) -> None:
        self._system.skip_pending_action(thread_id, action_id, action_command)

    def insert_action(
        self, thread_id: str, template: ActionTemplate,
        where: InsertPosition,
    ) -> None:
        self._system.insert_action(thread_id, template, where)


class ThreadFacade(IThreadFacade):
    """Concrete ThreadFacade implementation."""

    def __init__(self, runtime: IThreadRuntimeAccess, system: ISystem) -> None:
        self._runtime = runtime
        self._system = system
        self._mutation_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, thread_id: str) -> asyncio.Lock:
        """Lazily allocate a per-thread mutation lock.

        Locks are evicted when the thread reaches a terminal state
        (COMPLETED / STOPPED / ABORTED) via `on_thread_terminal_event`, so
        the dict size tracks the live thread count, not lifetime distinct
        thread count. Callers must only allocate for a thread that exists --
        an unknown id would never see a terminal event, leaking its entry.
        """
        lock = self._mutation_locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._mutation_locks[thread_id] = lock
        return lock

    def on_thread_terminal_event(self, event: RuntimeEvent) -> None:
        """Drop the per-thread mutation lock when the thread terminates.

        Subscribed by SystemRuntime to its system event bus during
        construction. THREAD.<id>.COMPLETED / STOPPED / ABORTED are
        terminal -- no further mutation can target this id, so the lock
        entry is dead weight. Locks have no useful post-completion state
        so we just pop; no archive list.
        """
        if event.entity_type == "THREAD" and event.status in _TERMINAL_THREAD_STATUSES:
            self._mutation_locks.pop(event.entity_id, None)

    # -- Reads ---------------------------------------------------------------

    def list(self, execution_id: str) -> List[ThreadSnapshot]:
        return self._runtime.list_threads(execution_id)

    def get(self, execution_id: str, thread_id: str) -> ThreadSnapshot:
        return self._runtime.get_thread_detail(execution_id, thread_id)

    def list_paused(self, execution_id: str) -> List[ThreadSnapshot]:
        return self._runtime.get_paused_threads(execution_id)

    # -- Pause / resume ------------------------------------------------------

    @dangerous(
        name="thread.pause",
        level=DangerLevel.OPERATOR,
        message="Request pause on thread '{thread_id}' in execution '{execution_id}'. "
                "Cooperative: takes effect at the next safe point between actions. "
                "If the thread is currently MOVING or EXECUTING_ACTION, pause will "
                "not fire until that operation completes.",
    )
    async def pause(
        self, execution_id: str, thread_id: str,
        reason: str | None = None,
    ) -> None:
        del reason  # consumed by @dangerous audit trail
        self._runtime.pause_thread(execution_id, thread_id)

    @dangerous(
        name="thread.resume",
        level=DangerLevel.OPERATOR,
        message="Resume manually-paused thread '{thread_id}' in execution "
                "'{execution_id}'. Error-paused threads are not affected; "
                "use thread.recover with a decision instead.",
    )
    async def resume(
        self, execution_id: str, thread_id: str,
        reason: str | None = None,
    ) -> None:
        del reason  # consumed by @dangerous audit trail
        self._runtime.resume_thread(execution_id, thread_id)

    @dangerous(
        name="thread.pause_all",
        level=DangerLevel.CRITICAL,
        message="Request pause on every active thread in execution '{execution_id}'. "
                "Threads pause independently at their next safe points; some may "
                "remain MOVING or EXECUTING_ACTION for minutes to hours.",
    )
    async def pause_all(
        self, execution_id: str,
        reason: str | None = None,
    ) -> dict[str, int]:
        del reason  # consumed by @dangerous audit trail
        return self._runtime.pause_execution(execution_id)

    @dangerous(
        name="thread.resume_all",
        level=DangerLevel.CRITICAL,
        message="Resume every manually-paused thread in execution "
                "'{execution_id}'. Also cancels pending pause requests "
                "queued by an earlier pause_all that have not yet fired, "
                "so threads which had not reached PAUSED at the time of "
                "the resume continue running instead of pausing later. "
                "If the execution has a quarantined orphaned backlog "
                "(ORPHANED_BACKLOG incident), this resume ALSO disposes of "
                "it irreversibly: the undelivered contributions are "
                "abandoned (accept-partial) and their threads continue.",
    )
    async def resume_all(
        self, execution_id: str,
        reason: str | None = None,
    ) -> dict[str, int]:
        del reason  # consumed by @dangerous audit trail
        return self._runtime.resume_execution(execution_id)

    @dangerous(
        name="thread.recover",
        level=DangerLevel.CRITICAL,
        message="Recover error-paused thread '{thread_id}' with decision '{decision}'. "
                "RETRY re-runs the failed action (fix the root cause first). "
                "RETRY_OP re-runs only the failed device call, leaving the rest of "
                "the action body suspended (valid only while a device op is paused); "
                "on a device that supports it the engine re-reads hardware state first "
                "and refuses the retry if only a human can resolve what it finds. "
                "CONTINUE carries on to the next action, recording this one as "
                "operator-confirmed rather than executed -- it promises nothing "
                "about what the failed action actually did. ABORT_ACTION discards "
                "the action and continues. ABORT_METHOD skips the rest of the "
                "method. ABORT_THREAD stops the thread entirely. "
                "RETRY, RETRY_OP and CONTINUE also say the device has been "
                "looked at, so they clear the fault this pause is about and "
                "no separate clear-device-fault is needed. The three aborts "
                "say nothing about the machine and leave the fault standing, "
                "which is when a plate can still be in the jaws.",
    )
    async def recover(
        self, execution_id: str, thread_id: str, decision: RecoveryDecision,
    ) -> None:
        # Validate membership BEFORE allocating the lock (as every other
        # mutation method does): _lock_for would otherwise cache an entry for a
        # mistyped id that no terminal event will ever evict, leaking the dict.
        self._validate_thread_in_execution(execution_id, thread_id)
        # Take the per-thread mutation lock so a recovery decision cannot
        # interleave with an in-flight mutation's await window (a mutation
        # validates pause state, then awaits its template build before
        # touching the lane; without this, a concurrent recover could unpark
        # the thread mid-build and the mutation would land on a moving lane).
        async with self._lock_for(thread_id):
            await self._clear_the_fault_this_pause_names(
                execution_id, thread_id, decision,
            )
            self._runtime.recover_thread(execution_id, thread_id, decision)

    async def _clear_the_fault_this_pause_names(
        self, execution_id: str, thread_id: str, decision: RecoveryDecision,
    ) -> None:
        """Drop the device's fault when the decision says the machine was dealt with.

        RETRY, RETRY_OP and CONTINUE all say the same thing about the
        instrument: it has been looked at and is fit to drive. The aborts say
        nothing about it, and giving up on the work is exactly when a plate can
        still be in the jaws.

        Cleared before the thread resumes, because a fault still standing
        refuses the call the operator just asked for. A decision the thread
        then rejects leaves it cleared, which is right: those refusals are
        about the ledger or the pause site, not about whether anyone looked.
        """
        if decision not in _DECISIONS_THAT_SAY_THE_DEVICE_IS_FIT:
            return
        fault = self._runtime.fault_named_by_pause(execution_id, thread_id)
        if fault is None:
            return
        await self._runtime.clear_fault_if_current(fault)

    # -- Mutation ------------------------------------------------------------

    @dangerous(
        name="thread.skip_method",
        level=DangerLevel.CRITICAL,
        message="Skip pending method on thread '{thread_id}'. If the skipped "
                "method's side effects (seal, wash, incubate) are required for "
                "downstream correctness, the workflow may fail later.",
        requires_reason=True,
    )
    async def skip_method(
        self, execution_id: str, thread_id: str, *,
        method_id: str | None = None,
        method_name: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Skip a pending method on a paused thread.

        method_name selects the FIRST pending method on the lane that
        matches; method names are not unique, so a single call skips
        only one match and re-issuing the call targets the next.

        method_id is accepted for backward compatibility with internal
        callers that already track ids (e.g. mutate_on_next_pause). The
        lane has NO id index -- under the hood, skip_pending_method
        falls back to `method_name or method_id`, so passing method_id
        alone is treated as a NAME lookup that matches a method whose
        name happens to equal that id (a legacy quirk, not a real id
        dispatch). Wire callers should pass method_name; the wire
        surface (REST schema + Protocol + CLI) no longer exposes
        method_id.
        """
        del reason  # consumed by @dangerous audit trail
        self._validate_thread_in_execution(execution_id, thread_id)
        async with self._lock_for(thread_id):
            self._system.skip_pending_method(thread_id, method_id, method_name)

    @dangerous(
        name="thread.abort_method",
        level=DangerLevel.CRITICAL,
        message="Abort the currently IN_PROGRESS method on thread '{thread_id}'. "
                "Aborts the running action and skips the rest of the method. "
                "Use skip_method for methods that have not yet started.",
        requires_reason=True,
    )
    async def abort_method(
        self, execution_id: str, thread_id: str, *,
        method_id: str | None = None,
        method_name: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Abort the IN_PROGRESS method on a paused thread.

        method_name selects the IN_PROGRESS method to abort. Names are
        not unique on the lane, but at most one method is IN_PROGRESS
        at a time so the first-match resolution is unambiguous here.

        method_id is accepted for backward compatibility (see
        skip_method docstring); the lane has no id index, so passing
        method_id alone falls back to a name lookup. Wire callers
        should pass method_name. The wire surface no longer exposes
        method_id.
        """
        del reason  # consumed by @dangerous audit trail
        self._validate_thread_in_execution(execution_id, thread_id)
        async with self._lock_for(thread_id):
            await self._system.abort_method(thread_id, method_id, method_name)

    @dangerous(
        name="thread.insert_method",
        level=DangerLevel.CRITICAL,
        message="Insert method '{template}' into thread '{thread_id}' at {where}. "
                "The inserted method runs in the order dictated by the position "
                "descriptor. Anchor-based positions wait silently if the anchor "
                "never appears; an incident is recorded at thread close.",
        requires_reason=True,
    )
    async def insert_method(
        self, execution_id: str, thread_id: str,
        template: MethodTemplate,
        where: InsertPosition, *,
        reason: str | None = None,
    ) -> None:
        """Insert a method on a paused thread.

        Routes through `ISystem.insert_method_async` so the async
        template generator is iterated on the caller's event loop --
        avoids the up-to-10s blocking thread-pool fallback used by the
        sync `insert_method` path (which only services
        `mutate_on_next_pause` callbacks).
        """
        del reason  # consumed by @dangerous audit trail
        self._validate_thread_in_execution(execution_id, thread_id)
        async with self._lock_for(thread_id):
            await self._system.insert_method_async(thread_id, template, where)

    @dangerous(
        name="thread.skip_action",
        level=DangerLevel.CRITICAL,
        message="Skip pending action on thread '{thread_id}'. Only valid while a "
                "method is IN_PROGRESS and the target action has not started yet.",
        requires_reason=True,
    )
    async def skip_action(
        self, execution_id: str, thread_id: str, *,
        action_id: str | None = None,
        action_command: str | None = None,
        reason: str | None = None,
    ) -> None:
        del reason  # consumed by @dangerous audit trail
        self._validate_thread_in_execution(execution_id, thread_id)
        async with self._lock_for(thread_id):
            self._system.skip_pending_action(thread_id, action_id, action_command)

    @dangerous(
        name="thread.insert_action",
        level=DangerLevel.CRITICAL,
        message="Insert action '{template}' into thread '{thread_id}' at {where}. "
                "Only valid while the assigned method is IN_PROGRESS; refused otherwise. "
                "Anchor (Before/After) matches ActionTemplate.tag and is "
                "scoped to the assigned method's action lane only -- tags "
                "are NOT validated for uniqueness across the system, so "
                "first-match-wins applies within that lane.",
        requires_reason=True,
    )
    async def insert_action(
        self, execution_id: str, thread_id: str,
        template: ActionTemplate,
        where: InsertPosition, *,
        reason: str | None = None,
    ) -> None:
        del reason  # consumed by @dangerous audit trail
        self._validate_thread_in_execution(execution_id, thread_id)
        async with self._lock_for(thread_id):
            self._system.insert_action(thread_id, template, where)

    @dangerous(
        name="thread.replace_method",
        level=DangerLevel.CRITICAL,
        message="Replace method '{target_name}' on thread '{thread_id}' with "
                "method '{template}'. A PENDING target is spliced in place. The "
                "IN_PROGRESS/errored target is STAGED to run next and returns "
                "staged_for_recovery=true -- drop the failed method via "
                "recover_thread(ABORT_METHOD) once the cell is physically safe.",
        requires_reason=True,
    )
    async def replace_method(
        self, execution_id: str, thread_id: str,
        target_name: str, template: MethodTemplate, *,
        reason: str | None = None,
    ) -> bool:
        del reason  # consumed by @dangerous audit trail
        self._validate_thread_in_execution(execution_id, thread_id)
        async with self._lock_for(thread_id):
            return await self._system.replace_method(thread_id, target_name, template)

    @dangerous(
        name="thread.replace_action",
        level=DangerLevel.CRITICAL,
        message="Replace action '{target_command}' on thread '{thread_id}' with "
                "action '{template}'. The replacement runs next. A PENDING "
                "target is also skipped; the IN_PROGRESS/errored target is "
                "STAGED and returns staged_for_recovery=true -- drop the failed "
                "action via recover_thread(ABORT_ACTION) once the cell is safe. "
                "Only valid while the assigned method is IN_PROGRESS.",
        requires_reason=True,
    )
    async def replace_action(
        self, execution_id: str, thread_id: str,
        target_command: str, template: ActionTemplate, *,
        reason: str | None = None,
    ) -> bool:
        del reason  # consumed by @dangerous audit trail
        self._validate_thread_in_execution(execution_id, thread_id)
        async with self._lock_for(thread_id):
            return self._system.replace_action(thread_id, target_command, template)

    @dangerous(
        name="thread.mutate_on_next_pause",
        level=DangerLevel.CRITICAL,
        message="Schedule callback to run when thread '{thread_id}' next reaches "
                "PAUSED. The callback receives a narrow IThreadMutationContext for "
                "variable and mutation ops; the thread auto-resumes after if it "
                "was manually paused.",
    )
    async def mutate_on_next_pause(
        self, execution_id: str, thread_id: str,
        callback: Callable[[IThreadMutationContext, str], None],
    ) -> None:
        ctx = _SystemMutationContext(self._system)

        def adapter(system: ISystem, tid: str) -> None:
            # `mutate_on_next_pause` on SystemRuntime hands the callback an
            # ISystem; we wrap it as the narrow mutation-context per spec.
            _ = system  # narrow context already captured
            callback(ctx, tid)

        self._runtime.mutate_on_next_pause(execution_id, thread_id, adapter)

    @dangerous(
        name="thread.spawn_thread",
        level=DangerLevel.OPERATOR,
        message="Manually create and start a thread from template '{template_name}' "
                "in execution '{execution_id}'. Used to recover from AUTO_SPAWN_FAILED "
                "incidents when the automatic spawn path could not find a matching "
                "thread template.",
    )
    async def spawn_thread(
        self, execution_id: str, template_name: str, *,
        labware_id: str | None = None,
    ) -> ThreadSnapshot:
        return await self._runtime.spawn_thread_in_execution(
            execution_id, template_name, labware_id=labware_id,
        )

    # -- Helpers -------------------------------------------------------------

    def _validate_thread_in_execution(self, execution_id: str, thread_id: str) -> None:
        """Raise KeyError if `thread_id` is not in `execution_id`'s threads.

        Prevents cross-execution mutation accidents. Daemon routes map
        KeyError to 404 so the CLI/MCP surface a clean "thread not found"
        instead of conflating it with a 400 precondition violation.
        Cheap because we already call this on every mutation and the
        executions dict is in-memory.
        """
        threads = self._runtime.list_threads(execution_id)
        if not any(t.id == thread_id for t in threads):
            raise KeyError(
                f"Thread '{thread_id}' is not part of execution '{execution_id}'"
            )
