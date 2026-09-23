import logging
from abc import ABC, abstractmethod
from orca.events.event_bus_interface import IEventBus
from orca.events.event_channel import EventChannelRegistry
from orca.events.execution_context import GroupLifecycleContext, WorkflowExecutionContext
from orca.system.reservation_manager.move_handler import MoveHandler
from orca.system.reservation_manager.interfaces import IThreadReservationCoordinator
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.system_map import SystemMap
from orca.system.thread_manager import ThreadManager
from orca.system.thread_manager_interface import IThreadManager
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.system.reservation_manager.errors import (
    IThreadIncidentDeclarer,
    OrphanedBacklogContext,
    ThreadDiedContext,
)
from orca.workflow_models.method import ExecutingMethod
from orca.workflow_models.status_enums import LabwareThreadStatus, MethodStatus, WorkflowStatus
from orca.workflow_models.status_manager import StatusManager
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflows.workflow import IWorkflow, WorkflowInstance


import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Dict, List, Protocol

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placement import LabwarePlacer
from orca.resource_models.location import Location
from orca.resource_models.labware_state import (
    ILabwareRegistry,
    IRegisteredThread,
    LabwareSlot,
)
from orca.resource_models.sharing import GroupSharing
from orca.runtime.group_execution_context import GroupExecutionContext
from orca.resource_models.capacity import OverflowAction
from orca.state.contents import LabwareContentsLedger
from orca.runtime.interfaces import ILabwareStore
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.submission import ResolvedAcquisition
from orca.workflow_models.labware_threads.executing_labware_thread import AutoSpawnCallback, CapacityPrecheckCallback
from orca.workflow_models.overflow_strategy import (
    refuse_the_spent_candidate,
    select_strategy,
)
from orca.workflow_models.workflows.workflow_registry import WorkflowRegistry


class _LabwareAdder(Protocol):
    """Minimal subset of ISystem the reuse-bind path consumes.

    ISystem cannot be imported here without closing a cycle through
    system_interface (which already imports IExecutingWorkflowRegistry from
    this module). The narrow Protocol keeps the constructor typed without
    pulling the cycle in.
    """

    def add_labware(self, labware: LabwareInstance) -> None: ...
    async def reconcile_lh_deck_occupancy(self, device_location: Location) -> None: ...
    @property
    def labware_placer(self) -> LabwarePlacer: ...
    @property
    def labware_contents(self) -> LabwareContentsLedger: ...

orca_logger = logging.getLogger("orca")

CreateThreadFn = Callable[
    [ThreadTemplate, ExecutingMethod | None, ResolvedAcquisition | None, WorkflowRunMode],
    Awaitable[LabwareThreadInstance],
]


class ExecutingWorkflow(IWorkflow):
    def __init__(self,
                 workflow: WorkflowInstance,
                 thread_reservation_coordinator: IThreadReservationCoordinator,
                 system_thread_manager: ThreadManager,
                 event_bus: IEventBus,
                 move_handler: MoveHandler,
                 status_manager: StatusManager,
                 system_map: SystemMap,
                 create_thread_fn: CreateThreadFn | None = None,
                 labware_registry: ILabwareRegistry | None = None,
                 labware_store: ILabwareStore | None = None,
                 system: _LabwareAdder | None = None,
                 ) -> None:
        self._workflow = workflow
        self._event_bus = event_bus
        self._labware_registry = labware_registry
        self._labware_store = labware_store
        self._system = system
        self._thread_reservation_coordinator = thread_reservation_coordinator
        self._thread_manager = system_thread_manager
        self._status_manager = status_manager
        self._move_handler = move_handler
        self._system_map = system_map
        self._create_thread_fn = create_thread_fn
        self._context = WorkflowExecutionContext(execution_id=self._workflow.id, workflow_name=self._workflow.name)
        self._event_channel_registry = EventChannelRegistry()
        self._entry_threads: List[ExecutingLabwareThread] = []
        auto_spawn_callback = self._make_auto_spawn_callback()
        capacity_precheck_callback = self._make_capacity_precheck_callback()
        for entry_thread in self._workflow.entry_threads:
            executing_thread = self._thread_manager.create_executing_thread(entry_thread.id, self._context)
            executing_thread.set_event_channel_registry(self._event_channel_registry)
            if auto_spawn_callback is not None:
                executing_thread.set_auto_spawn_callback(auto_spawn_callback)
            if capacity_precheck_callback is not None:
                executing_thread.set_capacity_precheck_callback(capacity_precheck_callback)
            if self._labware_registry is not None:
                executing_thread.set_labware_registry(self._labware_registry)
            executing_thread.set_work_finished_hook(self._on_thread_work_finished)
            self._entry_threads.append(executing_thread)
        self._spawned_threads: List[ExecutingLabwareThread] = []
        self._auto_spawn_callback = auto_spawn_callback
        self._capacity_precheck_callback = capacity_precheck_callback
        self._tick_loop_task: asyncio.Task[None] | None = None
        # Retained handle for every thread task this workflow schedules (entry,
        # auto-spawned, injected). Keyed by thread id so a re-spawn of the same
        # id overwrites rather than duplicates. ``stop_all_thread_tasks`` cancels
        # these on an abortive stop; without the handles, fire-and-forget
        # spawned-thread tasks would survive ``execution.task.cancel()`` and
        # leak reservations / re-drive stuck shared actions.
        self._thread_tasks: Dict[str, asyncio.Task[None]] = {}
        # Set while a pause is in force: a thread started after the pause
        # fan-out is not in it, so nothing else would hold it.
        self._new_threads_held = False
        self._new_threads_hold_reason = "manual"
        # Fires once entry threads have been scheduled as tasks and
        # workflow status is IN_PROGRESS, BEFORE awaiting their completion.
        # SystemRuntime gates the SubmissionStatus IN_PROGRESS transition on
        # this so the wire-shape "running" signal arrives promptly instead
        # of being held until every entry thread terminates (could be 80s+
        # on real hardware). The event remains set for the lifetime of the
        # workflow; consumers wait once and discard.
        self._entry_threads_started: asyncio.Event = asyncio.Event()
        # Tracks (submission_id, group_id) tuples whose GROUP.COMPLETED has
        # already been emitted; prevents double-firing as additional threads
        # in the same group terminate after the group's last member did.
        self._group_completed_emitted: set[tuple[str, str]] = set()
        # >0 while a submission is mid-injection; holds slot closes so a
        # mid-injection feeder terminal can't close a shared slot (see injecting()).
        self._pending_injections: int = 0
        self._injections_settled: asyncio.Event = asyncio.Event()
        self._injections_settled.set()
        # Cleared for good once wait_all_threads concludes: from then on this
        # workflow is finishing and cannot take another submission's threads.
        self._accepting_injections: bool = True
        self._incident_declarer: IThreadIncidentDeclarer | None = None
        # One-way teardown latch: parked receivers' STOPPED terminals land after
        # cleanup_parked_threads returns, so this is never cleared.
        self._tearing_down: bool = False
        self._subscribe_events()

    @property
    def id(self) -> str:
        return self._workflow.id
    
    @property
    def name(self) -> str:
        return self._workflow.name

    @property
    def thread_manager(self) -> ThreadManager:
        return self._thread_manager

    @property
    def event_bus(self) -> IEventBus:
        return self._event_bus
    
    @property
    def status(self) -> WorkflowStatus:
        status = self._status_manager.get_status(self._workflow.id)
        return WorkflowStatus[status]

    @status.setter
    def status(self, status: WorkflowStatus) -> None:
        self._status_manager.set_status("WORKFLOW", self._workflow.id, status.name, self._context)

    @property
    def entry_threads_started(self) -> asyncio.Event:
        """Fires when entry threads are scheduled and workflow is IN_PROGRESS.

        SystemRuntime awaits this between submitting and transitioning
        SubmissionStatus from ACCEPTED to IN_PROGRESS so operator-visible
        "running" lands promptly instead of being held until every entry
        thread completes.
        """
        return self._entry_threads_started

    async def start(self) -> None:
        """Run entry threads to completion. Errors raise after all complete.

        Three SRP-split phases:
          1. ``_begin_workflow_run`` flips workflow status to IN_PROGRESS
             and starts the tick loop.
          2. ``_schedule_entry_threads`` creates asyncio tasks for each
             entry thread.
          3. ``_await_entry_threads`` gathers the tasks and surfaces any
             startup errors as a uniform exception.

        ``entry_threads_started`` is fired via ``finally`` so a failure
        while starting (e.g. double-start RuntimeError) still unblocks any
        SystemRuntime caller waiting on the event. The caller then awaits
        the start coroutine itself to surface the underlying error,
        rather than hanging forever on a never-fired event.
        """
        try:
            self._begin_workflow_run()
            tasks = self._schedule_entry_threads()
        finally:
            self._entry_threads_started.set()
        await self._await_entry_threads(tasks)

    def _begin_workflow_run(self) -> None:
        """Validate the workflow is unstarted, start the tick loop, and
        transition status to IN_PROGRESS. Idempotency is rejected loudly
        rather than silently no-oping so double-starts surface as bugs."""
        self._tick_loop_task = asyncio.create_task(
            self._thread_reservation_coordinator.start_tick_loop()
        )
        if self.status != WorkflowStatus.CREATED:
            raise RuntimeError(
                f"Workflow {self._workflow.name} is already started or completed."
            )
        self.status = WorkflowStatus.IN_PROGRESS

    def _schedule_entry_threads(self) -> List[asyncio.Task[None]]:
        """Create one asyncio task per entry thread.

        ``asyncio.create_task`` schedules each coroutine on the event loop
        immediately; the tasks run concurrently when control yields. The
        returned list is awaited by ``_await_entry_threads`` so startup
        errors propagate uniformly.
        """
        tasks: List[asyncio.Task[None]] = []
        for thread in self._entry_threads:
            self._hold_if_held(thread)
            task = asyncio.create_task(thread.start())
            self._thread_tasks[thread.id] = task
            # Same record a spawned thread's crash leaves, filed the moment the
            # thread dies rather than when the gather below unwinds.
            task.add_done_callback(
                lambda t, th=thread: self._on_spawned_thread_done(t, th)
            )
            tasks.append(task)
        return tasks

    async def _await_entry_threads(
        self, tasks: List[asyncio.Task[None]],
    ) -> None:
        """Gather entry-thread tasks; on failure log and re-raise.

        ``return_exceptions=True`` so one thread's failure doesn't cancel
        siblings mid-startup and leak reservations. Single failure
        re-raises the original exception (matches pre-gather behavior).
        Multiple failures wrap into a summary ``RuntimeError`` because
        ``ExceptionGroup`` is 3.11+ and orca-core targets 3.10.

        Known limitation: this waits for EVERY entry thread, so an entry-thread
        failure does not surface until its siblings also finish. A sited sibling
        entry thread parked at co-labware for the failed thread's labware waits
        unbounded (``co_labware_timeout`` defaults to None), so the raise is
        deferred until an operator stop breaks that wait. The co-labware wait is
        not reservation acquisition, so the reservation deadlock detector does not
        see it. The common convergence pattern is unaffected -- ``orca.join``
        contributors are auto-spawned ``wf.thread`` threads (not entry threads)
        and escalate promptly via their done-callback -- so only multiple
        ``wf.start`` entry threads co-locating with mixed siting hit this.
        """
        results = await asyncio.gather(*tasks, return_exceptions=True)
        errors = [
            (thread, result)
            for thread, result in zip(self._entry_threads, results)
            if isinstance(result, BaseException)
        ]
        if not errors:
            return
        for thread, exc in errors:
            orca_logger.error(
                "Entry thread '%s' failed to start in workflow '%s': %s",
                thread.id, self._workflow.name, exc, exc_info=exc,
            )
        if len(errors) == 1:
            raise errors[0][1]
        raise RuntimeError(
            f"{len(errors)} entry thread(s) failed to start in workflow "
            f"'{self._workflow.name}': "
            + ", ".join(
                f"thread '{t.id}' raised {type(e).__name__}"
                for t, e in errors
            )
        ) from errors[0][1]

    async def _resolve_reuse_bind(
        self, template: ThreadTemplate, replaces_spent_labware: bool = False,
    ) -> ResolvedAcquisition | None:
        """Return a `ResolvedAcquisition` for a reuse-bound thread, or None.

        Default (non-reuse) threads short-circuit to None so the factory
        takes its create-fresh-from-template path, and so does a receiver
        replacing labware that was used up (``replaces_spent_labware``). Reuse-bound threads
        enter the location's `spawn_lock` and either bind to an existing
        matching labware (`created_fresh=False`) or create a fresh
        instance, register it in both `system.labwares` and the labware
        store, and wrap it (`created_fresh=True`). A wrong-template
        occupant raises `SpawnIncompatibleError`; a runtime constructed
        without `labware_store + system` raises
        `SpawnContextUnavailableError`.

        Extracted from `_make_auto_spawn_callback` because the inline block
        grew too long to read.
        """
        if not template.start_reuse_existing:
            return None
        if replaces_spent_labware:
            # Adopting the used-up labware again would deplete on this
            # receiver's first contribution and overflow straight back into
            # the same stash. A handoff for any OTHER reason still adopts it.
            return None
        # Local import avoids the runtime_interface cycle through
        # plugins/events that closes on system_interface.
        from orca.runtime.runtime_interface import (
            SpawnContextUnavailableError,
            SpawnIncompatibleError,
        )
        if self._labware_store is None or self._system is None:
            raise SpawnContextUnavailableError(
                f"Thread template '{template.name}' uses "
                f"start_reuse_existing but ExecutingWorkflow was "
                f"constructed without labware_store and system refs."
            )
        # start_loc is always a concrete site or pad: resolve_journey_location
        # rejects a device-endpoint start before a reuse thread reaches here.
        start_loc = template.start_location
        # The spawn_lock only needs to span the read-check-claim sequence.
        # Once `initialize_labware` writes `start_loc._labware = fresh`,
        # the next contender's `start_loc.labware is not None` check sees
        # the new labware and binds instead of creating another. Holding
        # the lock across the slow store I/O serializes every spawn-bind
        # on a DB-backed store; restructure so the I/O happens after we
        # release the lock (review item L8).
        async with start_loc.spawn_lock:
            existing = start_loc.labware
            if existing is not None:
                if existing.template_name != template.labware_template.name:
                    raise SpawnIncompatibleError(
                        location=start_loc.position_id,
                        expected_template=template.labware_template.name,
                        actual_template=existing.template_name,
                    )
                # Already bound; the deck was reconciled when it first bound.
                return ResolvedAcquisition(
                    labware_instance=existing, created_fresh=False,
                )
            # Rebind a resident to its stable id (vs minting fresh). The store
            # read is in-lock so read-check-claim is atomic; residents are rare (DB hit ok).
            persisted = await self._labware_store.get_by_position(start_loc.position_id)
            if persisted is not None:
                if persisted.template_name != template.labware_template.name:
                    raise SpawnIncompatibleError(
                        location=start_loc.position_id,
                        expected_template=template.labware_template.name,
                        actual_template=persisted.template_name,
                    )
                start_loc.initialize_labware(persisted)
                bound, created_fresh = persisted, False
            else:
                bound = await template.labware_template.create_instance()
                # Synchronous slot claim: future contenders take the bind branch.
                start_loc.initialize_labware(bound)
                created_fresh = True
        # Slot is claimed; bookkeeping outside the lock so other locations /
        # threads aren't blocked by our DB round-trip.
        self._system.add_labware(bound)
        # Before anything projects it onto a deck. A rack that reaches the
        # driver with nothing written about its tips gets the driver's own
        # default, which is a full rack.
        await bound.enter_record(self._system.labware_contents)
        if created_fresh:
            # Persist identity + position: execution_id is teardown-diff metadata;
            # position lets a restart rehydrate it and the next bind rebind to this id.
            await self._labware_store.register(bound, execution_id=self._workflow.id)
            await self._labware_store.update_location(bound.id, start_loc.position_id)
        # Slot already claimed in-lock; write the remaining holders without
        # re-writing the slot (re-staging a bridge would raise).
        await self._system.labware_placer.bind_resident(bound, start_loc)
        return ResolvedAcquisition(
            labware_instance=bound, created_fresh=created_fresh,
        )

    async def _refuse_an_adopted_spent_labware(
        self,
        slot: LabwareSlot,
        template: ThreadTemplate,
        resolved: ResolvedAcquisition | None,
        contributor_method: ExecutingMethod,
    ) -> None:
        """Refuse when the labware just adopted from the deck is already used up.

        A receiver REPLACING spent labware never adopts, so this only ever fires
        on the adopt: the first contribution into the slot, against whatever was
        standing there when the run started. Before it, that labware was bound
        without being asked, and the shortfall surfaced inside the action body,
        at the instrument, with no replacement asked for.

        The refusal, rather than a replacement, because a reuse-bound thread has
        no route in. A replacement receiver would ask for a placement at a site
        the spent labware still physically occupies, and nothing here can take it
        off: no thread owns it, so no declared removal runs. So the contributor
        pauses and the operator is told both halves.

        Only an ADOPTED labware is asked. A freshly minted one holds what its
        template declares, and a labware nothing has ever described answers that
        it can continue: unknown is not empty, so it binds and the operator
        settles what it holds.
        """
        if resolved is None or resolved.created_fresh:
            return
        candidate = resolved.labware_instance
        if candidate is None:
            return
        demand = contributor_method.aggregate_demand(candidate.template_name)
        if await candidate.can_continue(demand):
            return
        start_location = template.start_location
        where = (
            start_location.position_id if start_location is not None else slot.slot_key
        )
        refuse_the_spent_candidate(
            slot, self,
            f"{candidate.name!r} at {where} was already used up when this run "
            f"started, so it has nothing for {contributor_method.name!r}. Take "
            f"it off {where}, put a fresh one there, state what it holds, then "
            f"retry.",
        )

    def _make_auto_spawn_callback(self) -> AutoSpawnCallback | None:

        if self._create_thread_fn is None:
            return None
        if self._workflow.template is None:
            return None

        async def callback(
            labware_name: str,
            shared_method: ExecutingMethod,
            run_mode: WorkflowRunMode,
        ) -> None:
            assert self._workflow.template is not None
            assert self._labware_registry is not None, (
                "ExecutingWorkflow requires a labware registry. "
                "Factory must provide InMemoryLabwareRegistry or equivalent."
            )
            template = self._workflow.template.require_auto_spawn_template(labware_name)

            # T6: the contributor thread's group/submission context (if any)
            # was stashed on shared_method in _auto_spawn_for_action. Pass it
            # to slot_key_for so GroupAwareLabwareRegistry can compose the
            # per-(template, group, submission) key.
            ctx = shared_method.contributor_context
            slot_key = self._labware_registry.slot_key_for(template, ctx)
            slot = self._labware_registry.get_or_create_slot(slot_key, labware_name)
            slot.on_awaiting_join = self._on_receiver_awaiting_join

            if slot.orphaned:
                # Quarantined: never bind or mint; the execution-level resume
                # disposes of the stash with the rest (accept-partial).
                slot.pending.append(shared_method)
                return

            # Under slot.spawn_lock: check-then-create must be atomic per slot.
            async with slot.spawn_lock:
                if slot.has_active_thread():
                    active = slot.active_thread
                    assert active is not None
                    active_lw = active.thread_instance.labware
                    demand = shared_method.aggregate_demand(active_lw.template_name)
                    can_continue = await active_lw.can_continue(demand)
                    # Re-validate after the await: can_continue() may have drained
                    # the receiver, so binding below would land on a leaving thread.
                    if not slot.has_active_thread():
                        pass  # fall through to fresh-receiver branch below
                    elif can_continue and slot.has_room():
                        lt = active.labware_template
                        if lt is not None:
                            shared_method.assign_thread(lt, active.thread_instance)
                        shared_method.set_pool_index(
                            labware_name, slot.contributions_to_active,
                        )
                        slot.queue.put_nowait(shared_method)
                        slot.contributions_to_active += 1
                        return
                    else:
                        # A full receiver already ends on its own; a spent one
                        # has no other way to hear its labware is used up.
                        if not can_continue:
                            slot.mark_receiver_spent()
                        select_strategy(slot).on_overflow(slot, shared_method, self)
                        return

                # Quarantine can land during the awaits above; re-check in-lock
                # so the fall-through stashes, never mints into the backlog.
                if slot.orphaned:
                    slot.pending.append(shared_method)
                    return

                # No active thread: first contributor mints the receiver, inheriting its
                # group/submission identity (group_id cleared for SHARED_ACROSS_GROUPS).
                assert self._create_thread_fn is not None
                # Runs under slot.spawn_lock, so _resolve_reuse_bind's internal L8
                # Location.spawn_lock release no longer widens concurrency for same-slot spawns.
                resolved = await self._resolve_reuse_bind(
                    template, replaces_spent_labware=slot.receiver_spent,
                )
                await self._refuse_an_adopted_spent_labware(
                    slot, template, resolved, shared_method,
                )
                thread_instance = await self._create_thread_fn(
                    template, None, resolved, run_mode,
                )
                if ctx is not None:
                    is_shared = (
                        template.labware_template.group_sharing
                        is GroupSharing.SHARED_ACROSS_GROUPS
                    )
                    thread_instance.set_group_id(None if is_shared else ctx.group_id)
                    thread_instance.set_submission_id(ctx.submission_id)
                    thread_instance.set_batch_mode(ctx.batch_mode)
                shared_method.assign_thread(template.labware_template, thread_instance)
                shared_method.set_pool_index(labware_name, 0)
                slot.queue.put_nowait(shared_method)
                executing_thread = self.add_thread(thread_instance)
                slot.active_thread = executing_thread
                slot.contributions_to_active = 1
                # Reset drained for the fresh receiver: a stale True here would make
                # every later contributor spawn another receiver (ghost cascade).
                slot.receiver_drained = False
                slot.receiver_spent = False
                slot.policy = self._workflow.template.get_capacity_policy(
                    template.labware_template.name
                )
                slot.overflow_strategy = self._workflow.template.get_overflow_strategy(
                    template.labware_template.name
                )
                if not executing_thread.manual_start:
                    self.start_thread(executing_thread)

        return callback

    def _make_capacity_precheck_callback(self) -> CapacityPrecheckCallback | None:
        """Build the side-effect-free pre-check used by _auto_spawn_for_action.

        Mirrors the active-receiver capacity branch of the spawn callback but
        commits nothing about the CONTRIBUTION: no queue puts, no
        contributions_to_active increment, no slot.pending mutation. It does
        record depleted labware on the slot, which is a fact about the
        receiver rather than a routing decision. If the spawn would overflow
        with OverflowAction.RECOVERABLE_REJECT, invokes the slot's strategy
        (which emits AWAITING_DECISION and raises) so the action-loop wrapper
        can pause the contributor before any partial multi-input commit.
        """
        if self._workflow.template is None or self._labware_registry is None:
            return None

        async def precheck(labware_name: str, ctx: object | None,
                           contributor_method: ExecutingMethod) -> None:
            assert self._workflow.template is not None
            assert self._labware_registry is not None
            template = self._workflow.template.get_auto_spawn_template(labware_name)
            if template is None:
                return
            slot_key = self._labware_registry.slot_key_for(template, ctx)
            slot = self._labware_registry.get_slot(slot_key)
            if slot is None or not slot.has_active_thread():
                return
            active = slot.active_thread
            assert active is not None
            active_lw = active.thread_instance.labware
            demand = contributor_method.aggregate_demand(active_lw.template_name)
            can_continue = await active_lw.can_continue(demand)
            # Re-validate after the await: the receiver may have exhausted
            # its user generator during can_continue() and flipped the
            # slot's drained flag. Pre-check is side-effect-free; if the
            # receiver is now drained we have nothing to overflow against.
            if not slot.has_active_thread():
                return
            if can_continue and slot.has_room():
                return
            if not can_continue:
                # Not a commitment about this contribution: a statement about
                # the receiver. Under RECOVERABLE_REJECT the strategy below
                # raises, so the commit callback never runs and this is the
                # only place a depleted receiver would ever hear about it.
                slot.mark_receiver_spent()
            if slot.policy is None:
                return
            if slot.policy.overflow_action is not OverflowAction.RECOVERABLE_REJECT:
                return
            select_strategy(slot).on_overflow(slot, contributor_method, self)

        return precheck

    def _subscribe_events(self) -> None:
        for event in self._workflow.event_hooks:
            self._event_bus.subscribe(event.event_name, event.handler)

    def add_thread(self, thread: LabwareThreadInstance) -> ExecutingLabwareThread:
        """Create executing thread, track it, and fire THREAD.CREATED."""
        executing_thread = self._thread_manager.create_executing_thread(thread.id, self._context)
        executing_thread.set_event_channel_registry(self._event_channel_registry)
        if self._auto_spawn_callback is not None:
            executing_thread.set_auto_spawn_callback(self._auto_spawn_callback)
        if self._capacity_precheck_callback is not None:
            executing_thread.set_capacity_precheck_callback(self._capacity_precheck_callback)
        if self._labware_registry is not None:
            executing_thread.set_labware_registry(self._labware_registry)
        executing_thread.set_work_finished_hook(self._on_thread_work_finished)
        self._spawned_threads.append(executing_thread)
        return executing_thread

    def add_entry_threads(self, new_entry_threads: List[LabwareThreadInstance]) -> None:
        """Attach entry threads to a running ExecutingWorkflow.

        Used by SystemRuntime._inject_submission to deliver a second
        submission's groups into an already-booted execution. Each thread
        must already be registered with the system's ThreadRegistry
        (typically via system.add_thread(...)) before calling this.

        Each new thread is wrapped via add_thread (which wires callback +
        labware registry + terminal hook) and started as a task on the
        running event loop. The wrapper appends to _spawned_threads, which
        wait_all_threads already polls for late additions.
        """
        for entry_thread in new_entry_threads:
            executing_thread = self.add_thread(entry_thread)
            self._hold_if_held(executing_thread)
            task = asyncio.create_task(executing_thread.start())
            self._thread_tasks[executing_thread.id] = task

    def _on_thread_work_finished(self, thread: ExecutingLabwareThread) -> None:
        """A thread stopped being able to contribute: it went terminal, or it
        parked waiting for an operator to collect its labware.

        Quarantines any slot orphaned by an abnormal receiver death, then
        re-evaluates open receiver slots for close eligibility, then fires
        GROUP.{group_id}.COMPLETED when all threads of a given
        (submission_id, group_id) pair have reached terminal (idempotent via
        ``_group_completed_emitted``). Only the close evaluation is due on the
        collection park: the quarantine branch reads terminal states, and the
        group-completed check reads ``has_completed``, which that park is not.
        """
        # FAILED belongs with the rest: a receiver that died owing work strands
        # its backlog whether it was aborted or crashed.
        if thread.status in (
            LabwareThreadStatus.ABORTED,
            LabwareThreadStatus.STOPPED,
            LabwareThreadStatus.FAILED,
        ):
            try:
                self._quarantine_orphaned_slots(thread)
            except Exception:
                # A quarantine bug must not convert a routine operator abort
                # into an execution FAILURE via the done-callback chain.
                orca_logger.exception(
                    "Quarantine sweep failed for terminal thread %s", thread.name,
                )
        self._evaluate_slot_closures()
        self._maybe_fire_group_completed(thread)

    def _quarantine_orphaned_slots(self, dead: ExecutingLabwareThread) -> None:
        """Quarantine every slot whose active receiver just died owing work.

        Fully synchronous (atomic on the event loop). Backlog methods are
        bound to the dead thread, so re-routing them is never legal (the
        DoubleAssignment class): the slot is flagged ``orphaned``, in-scope
        threads are pause-requested, and an ORPHANED_BACKLOG incident points
        the operator at the execution-level resume (accept-partial). A
        DRAINED receiver is excluded: its generator already exhausted, it
        died owing nothing, and its leftovers belong to the successor-handoff
        flow (a successor mint may already be in flight under spawn_lock).
        Join waiters are excluded from the pause fan-out (an empty-handed
        ``await_next_method`` races only the stop event and cannot pause), and
        so are STOPPING threads (a latched pause would wedge their stop). An
        EMPTY pause set skips the quarantine entirely: everyone in scope is
        dying, done, or waiting -- a whole-teardown, not an orphaned
        convergence with live owners to protect.
        """
        if self._tearing_down or self._labware_registry is None:
            return
        in_flight = dead.assigned_method
        if in_flight is not None and (
            in_flight.completed.is_set()
            or not in_flight.shared_coord.is_contributor(dead.id)
        ):
            in_flight = None
        all_slots = list(self._labware_registry.all_slots().values())
        awaiting = [w for s in all_slots for w in s.awaiting_threads]
        for slot in all_slots:
            if slot.orphaned or slot.active_thread is not dead or slot.receiver_drained:
                continue
            undelivered = slot.undelivered_count()
            if undelivered == 0 and in_flight is None:
                continue
            group_scope, submission_scope = self._labware_registry.slot_scope(slot.slot_key)
            to_pause = [
                t for t in self.threads
                if t is not dead
                and not t.has_completed()
                and t.status is not LabwareThreadStatus.STOPPING
                and self._thread_in_scope(t, group_scope, submission_scope)
                and not any(t is w for w in awaiting)
            ]
            # Nobody to protect: every in-scope peer is dying, done, or a join
            # waiter -- a whole-teardown stop, not an orphaned convergence.
            if not to_pause:
                continue
            slot.orphaned = True
            slot.orphaned_in_flight = in_flight
            paused: list[str] = []
            pause_message = (
                f"orphaned backlog: slot '{slot.slot_key}' quarantined, "
                f"{undelivered} contribution(s) undelivered"
            )
            for t in to_pause:
                t.request_pause(reason="system", message=pause_message)
                paused.append(t.id)
            self._event_bus.emit(f"SLOT.{slot.slot_key}.ORPHANED", self._context)
            orca_logger.info(
                "Slot orphaned [%s]: receiver %s %s, %d undelivered%s; "
                "pause requested: %s",
                slot.slot_key, dead.name, dead.status.name, undelivered,
                f", in-flight {in_flight.name}" if in_flight is not None else "",
                ", ".join(paused) or "none",
            )
            if self._incident_declarer is None:
                continue
            try:
                self._incident_declarer.declare_orphaned_backlog(
                    self._workflow.id,
                    OrphanedBacklogContext(
                        slot_key=slot.slot_key,
                        labware_template_name=slot.labware_template_name,
                        receiver_thread_id=dead.id,
                        receiver_thread_name=dead.name,
                        receiver_status=dead.status.name,
                        undelivered_count=undelivered,
                        in_flight_method_name=(
                            in_flight.name if in_flight is not None else None
                        ),
                        pause_requested_thread_ids=tuple(paused),
                    ),
                )
            except Exception:
                orca_logger.exception(
                    "Failed to record ORPHANED_BACKLOG for slot %s", slot.slot_key,
                )

    async def drain_orphaned_slots(self) -> None:
        """Accept-partial disposal, run on execution-level resume.

        Per slot, ALL slot mutation is one synchronous step (pop the backlog,
        clear the quarantine, reset, close) so late stashes and a second
        drain cannot interleave; the popped methods are then disposed of
        CONCURRENTLY (an abort can block on ``resolving_action_lock`` held
        across an hours-long reservation wait; its ``exit_signal`` is set
        before that lock, so parked owners are already released). Methods
        owned by an error-paused participant or with an action mid-execution
        are left to their own recovery channels.
        """
        if self._labware_registry is None:
            return
        to_dispose: list[ExecutingMethod] = []
        for slot in self._labware_registry.all_slots().values():
            if not slot.orphaned:
                continue
            collected: list[ExecutingMethod] = []
            while not slot.queue.empty():
                item = slot.queue.get_nowait()
                # Sentinel reachable: a slot can close before its receiver dies.
                if not isinstance(item, ExecutingMethod):
                    continue
                if item.completed.is_set():
                    continue
                collected.append(item)
            while slot.pending:
                method = slot.pending.popleft()
                if not method.completed.is_set():
                    collected.append(method)
            if slot.orphaned_in_flight is not None:
                if not slot.orphaned_in_flight.completed.is_set():
                    collected.append(slot.orphaned_in_flight)
                slot.orphaned_in_flight = None
            slot.orphaned = False
            slot.active_thread = None
            slot.contributions_to_active = 0
            slot.receiver_drained = False
            slot.receiver_spent = False
            slot.close()
            orca_logger.info(
                "Accept-partial drain [%s]: %d undelivered method(s) collected",
                slot.slot_key, len(collected),
            )
            to_dispose.extend(collected)
        if to_dispose:
            await asyncio.gather(
                *(self._dispose_orphaned_method(m) for m in to_dispose)
            )

    async def _dispose_orphaned_method(self, method: ExecutingMethod) -> None:
        """Abort one orphaned method; skip those owned by another recovery channel.

        The skip checks and ``abort()``'s synchronous prefix (where the
        user-task cancel lives) run in one sync segment, so an owner cannot
        reach ``execute()`` between check and abort.
        """
        error_paused = any(
            t.is_error_paused
            for t in self.threads
            if t.id in method.participating_thread_ids
        )
        if error_paused:
            orca_logger.info(
                "Accept-partial: leaving '%s' to error recovery", method.name,
            )
            return
        current = method.current_action
        if current is not None and current.action.user_task_running:
            orca_logger.info(
                "Accept-partial: '%s' is mid-execution; left to complete",
                method.name,
            )
            return
        if method.status is MethodStatus.CREATED:
            method.mark_skipped()
            return
        await method.abort()

    @asynccontextmanager
    async def injecting(self) -> AsyncIterator[None]:
        """Hold slot closes while a submission is being injected.

        A joining submission's feeder threads are accepted before they are
        registered live (an ``await`` for acquisition resolution then entry-thread
        build). If an existing feeder terminates in that window,
        ``_evaluate_slot_closures`` would see no live feeder for a shared
        receiver's slot and close it prematurely, stranding the joining feeder
        into a duplicate receiver. Guarding the window defers such closes; the
        ``finally`` re-evaluates once the joining feeders are live so any close
        legitimately due during the window still fires. Nests via the counter.
        """
        self._pending_injections += 1
        self._injections_settled.clear()
        try:
            yield
        finally:
            self._pending_injections -= 1
            if self._pending_injections == 0:
                self._injections_settled.set()
            self._evaluate_slot_closures()

    def _on_receiver_awaiting_join(self) -> None:
        """Await-transition trigger, wired onto every slot via ``on_awaiting_join``.

        A receiver starting an empty-handed join wait can complete an "all
        in-scope threads waiting or done" state, and no thread terminal fires
        at that moment; without this trigger the close that has become due
        would never be evaluated and the workflow would wait forever.
        """
        self._evaluate_slot_closures()

    def _evaluate_slot_closures(self) -> None:
        """Close every open receiver slot whose contribution window is over.

        A slot closes when either:
        - it declares feeders via ``contributes_to=[<labware name>]`` and none
          of its in-scope transitive feeders is still live (a live upstream
          thread that will still produce a feeder holds the slot open); or
        - it has NO declared feeder and no in-scope WORKER remains.

        "Live" here is ``has_finished_its_work``, not ``has_completed``: a
        thread parked waiting for an operator to collect its labware has made
        every contribution it will make. Counting it live held the receiver
        open, and every labware joined to that receiver on its deck slot,
        until someone walked over to the bench.

        A worker is any live in-scope thread that is not currently an
        empty-handed AWAITING-JOIN receiver of an open slot. Such receivers
        (suspended in ``await_next_method`` with nothing queued) are purely
        reactive: they resolve no actions, so they can demand nothing and hold
        nothing open. An EXECUTING thread counts as a worker for EVERY slot,
        receiver or not: which methods it will still yield is opaque (the
        engine never introspects generator bodies), so any live non-waiting
        thread may still demand any labware. A receiver awaiting a join on a
        CLOSED slot also counts as a worker: the close sentinel guarantees it
        wakes, and it may yield post-loop demand.

        The verdicts are insertion-order-free, which takes three rules because
        closes are one-way and closing a slot with a waiting receiver LIFTS
        that receiver's exclusion (awaiting-join on a CLOSED slot = worker):
        (1) each pass runs the FEEDER sweep first -- feeder decisions read
        only thread liveness, so they are mutually order-independent -- and
        restarts if any fired; (2) within the worker sweep, slots whose close
        can lift an exclusion (those holding a waiting receiver) are decided
        BEFORE slots that cannot (e.g. an open slot whose receiver already
        drained), so a lift is never forfeited to iteration order; (3) the
        worker sweep restarts on its FIRST close, so every later decision
        sees the rebuilt exclusion set. Guarantee stated precisely: no slot's
        verdict is taken before every close that could lift an exclusion
        affecting it has fired. In the symmetric mutual-hold case WHICH
        waiting-receiver slot closes first still follows iteration order --
        both choices are correct, exactly one closes per evaluation, and the
        wake cascade converges the rest. Terminates in at most
        open-slot-count restarts and is monotone-safe (a close only shrinks
        the excluded set, holding remaining slots open MORE readily). Fired
        on every thread terminal, every empty-handed join wait
        (``_on_receiver_awaiting_join``), and injection-window exit; held
        while a submission injection is in flight.

        Liveness is scoped to the slot's own group/submission (decoded from the
        slot key): a sibling submission's threads never hold an isolated slot
        open. A no-feeder receiver closes as soon as its in-scope threads are
        all waiting or done -- nothing is held open waiting for a submission
        that may never come (explicit hold-across-a-gap is a separate opt-in).

        The worker branch remains the termination guarantee for orphaned
        ``while ctx.has_more_work()`` receivers: demand can only originate
        from a live non-waiting thread, so once everything in scope is waiting
        or done the close always fires, and the sentinel wake cascades the
        remaining waiting receivers out one per wake.

        Closing is idempotent and only wakes a receiver blocked on its slot;
        in-progress work is unaffected, and work queued behind the close
        sentinel is still served (see ``LabwareSlot.close``). Quarantined
        (``orphaned``) slots are excluded entirely: they neither close nor
        decide until the operator's execution-level resume disposes of the
        backlog.

        Limitation: closure remains a prediction. ``contributes_to`` stays the
        author's precision channel for supply chains (a declared feeder chain
        holds a receiver open across gaps parked-discrimination cannot see).
        """
        if self._labware_registry is None or self._workflow.template is None:
            return
        if self._pending_injections > 0:
            return
        template = self._workflow.template
        while True:
            open_slots = [
                slot for slot in self._labware_registry.all_slots().values()
                if not slot.is_closed and not slot.orphaned
            ]
            if not open_slots:
                return
            # Identity list (not a set): mirrors the `t is p` comparison and
            # keeps unhashable test stubs usable as threads.
            awaiting_join = [
                w for slot in open_slots for w in slot.awaiting_threads
            ]
            feeder_closed = False
            worker_slots: list[LabwareSlot] = []
            for slot in open_slots:
                # Scope liveness to the slot's own group/submission: a sibling
                # submission's feeder must not hold an isolated receiver open.
                group_scope, submission_scope = self._labware_registry.slot_scope(slot.slot_key)
                feeder_set = template.transitive_feeders_for(slot.labware_template_name)
                if not feeder_set:
                    worker_slots.append(slot)
                    continue
                feeder_live = any(
                    t.thread_instance.thread_template is not None
                    and t.thread_instance.thread_template.name in feeder_set
                    and not t.has_finished_its_work()
                    and self._thread_in_scope(t, group_scope, submission_scope)
                    for t in self.threads
                )
                if not feeder_live:
                    self._log_close(slot, "FEEDER-dead", group_scope,
                                    submission_scope, awaiting_join)
                    slot.close()
                    feeder_closed = True
            if feeder_closed:
                continue
            # Decide exclusion-LIFTING slots first: only closing a slot that
            # holds a waiting receiver can turn that receiver into a worker.
            lift_first: list[LabwareSlot] = []
            rest: list[LabwareSlot] = []
            for slot in worker_slots:
                (lift_first if slot.awaiting_threads else rest).append(slot)
            worker_closed = False
            for slot in lift_first + rest:
                group_scope, submission_scope = self._labware_registry.slot_scope(slot.slot_key)
                worker_live = any(
                    not t.has_finished_its_work()
                    and not any(t is p for p in awaiting_join)
                    and self._thread_in_scope(t, group_scope, submission_scope)
                    for t in self.threads
                )
                if worker_live:
                    continue
                self._log_close(slot, "WORKER-QUIESCED", group_scope,
                                submission_scope, awaiting_join)
                slot.close()
                # First close only: later decisions must see the rebuilt
                # exclusion set (this receiver now counts as a worker).
                worker_closed = True
                break
            if not worker_closed:
                return

    def _log_close(
        self,
        slot: LabwareSlot,
        branch: str,
        group_scope: str | None,
        submission_scope: str | None,
        awaiting_join: list[IRegisteredThread],
    ) -> None:
        """One INFO line per close decision: the branch, the slot, and the
        in-scope thread snapshot the decision was taken on. Closure decisions
        are otherwise invisible at default log levels, which hid the
        premature-close defect class for months. ("awaiting-join" here is the
        thread's wait state, NOT the labware PARKED state / orca.park().)"""
        states = ", ".join(
            (t.thread_instance.thread_template.name
             if t.thread_instance.thread_template is not None else "<untemplated>")
            + "="
            + ("terminal" if t.has_completed()
               else "awaiting-collection" if t.has_finished_its_work()
               else "awaiting-join" if any(t is p for p in awaiting_join)
               else "executing")
            for t in self.threads
            if self._thread_in_scope(t, group_scope, submission_scope)
        )
        orca_logger.info(
            f"Slot close [{branch}] {slot.slot_key}: in-scope: {states or 'none'}"
        )

    @staticmethod
    def _thread_in_scope(
        thread: ExecutingLabwareThread,
        group_scope: str | None,
        submission_scope: str | None,
    ) -> bool:
        """True if the thread shares the slot's group/submission scope.

        A None scope is a wildcard (shared slot) matching any thread; a
        concrete scope matches only threads of that group/submission.
        """
        instance = thread.thread_instance
        if group_scope is not None and instance.group_id != group_scope:
            return False
        if submission_scope is not None and instance.submission_id != submission_scope:
            return False
        return True

    def _maybe_fire_group_completed(self, thread: ExecutingLabwareThread) -> None:
        """Fire GROUP.{id}.COMPLETED when all members of the group are terminal."""
        instance = thread.thread_instance
        group_id = instance.group_id
        submission_id = instance.submission_id
        if group_id is None or submission_id is None:
            return
        key = (submission_id, group_id)
        if key in self._group_completed_emitted:
            return
        any_live = any(
            t.thread_instance.group_id == group_id
            and t.thread_instance.submission_id == submission_id
            and not t.has_completed()
            for t in self.threads
        )
        if any_live:
            return
        self._group_completed_emitted.add(key)
        ctx = GroupLifecycleContext(
            execution_id=self._workflow.id,
            workflow_name=self._workflow.name,
            submission_id=submission_id,
            group_id=group_id,
        )
        self._event_bus.emit(f"GROUP.{group_id}.COMPLETED", ctx)

    @property
    def threads(self) -> List[ExecutingLabwareThread]:
        """All threads in this workflow (entry + spawned)."""
        return list(self._entry_threads) + list(self._spawned_threads)

    @property
    def spawned_threads(self) -> List[ExecutingLabwareThread]:
        return list(self._spawned_threads)

    @property
    def event_channel_registry(self) -> EventChannelRegistry:
        """Per-execution event channel registry (manual-step tracking, ctx.emit/wait_for)."""
        return self._event_channel_registry

    def set_event_channel_registry(self, registry: EventChannelRegistry) -> None:
        """Adopt an externally-built registry and re-propagate it to entry threads.

        The standalone executor pre-builds a method context (and its registry)
        before this workflow exists; adopting the same instance keeps the
        method's generator-body emit on the channels threads read.
        """
        self._event_channel_registry = registry
        for executing_thread in self._entry_threads:
            executing_thread.set_event_channel_registry(registry)

    def get_active_reservations(self) -> list[tuple[str, str, str | None]]:
        """Returns list of (position_id, reservation_id, thread_id) for active reservations.

        ``thread_id`` is None for reservations not owned by any executing
        thread (system-held / manual holds).
        """
        return self._thread_reservation_coordinator.get_active_reservations()

    def get_reservation_at(self, position_id: str) -> LocationReservation | None:
        """The reservation holding ``position_id``, or None."""
        return self._thread_reservation_coordinator.get_reservation_at(position_id)

    def cancel_reservation_by_id(self, reservation_id: str) -> tuple[str, str | None]:
        """Cancel an active reservation by id. Delegates to the coordinator,
        which does not know about executions -- callers must verify ownership
        (see `SystemRuntime.cancel_reservation`). Returns (position_id,
        thread_id) of the cancelled reservation; thread_id is None for
        reservations with no owning thread.
        """
        return self._thread_reservation_coordinator.cancel_reservation_by_id(reservation_id)

    def set_incident_declarer(self, declarer: IThreadIncidentDeclarer) -> None:
        """Wire the runtime back-ref so quarantine can record ORPHANED_BACKLOG."""
        self._incident_declarer = declarer

    def has_orphaned_slots(self) -> bool:
        if self._labware_registry is None:
            return False
        return any(s.orphaned for s in self._labware_registry.all_slots().values())

    def orphaned_labware_template_names(self) -> set[str]:
        if self._labware_registry is None:
            return set()
        return {
            s.labware_template_name
            for s in self._labware_registry.all_slots().values()
            if s.orphaned
        }

    def start_thread(
        self,
        executing_thread: ExecutingLabwareThread,
        *,
        honor_pause_hold: bool = True,
    ) -> None:
        """Start a spawned thread (must already be tracked via add_thread).

        Attaches a done-callback that surfaces the fire-and-forget task's
        exception (else a crash inside ``start()`` dies on the task object and
        the thread sits in ``CREATED`` forever).

        ``honor_pause_hold`` (default True) makes the thread start paused while
        a pause is in force. Operator spawn-recovery passes False: injecting a
        thread into a paused run is exactly what that call is for.
        """
        if honor_pause_hold:
            self._hold_if_held(executing_thread)
        task = asyncio.get_running_loop().create_task(executing_thread.start())
        self._thread_tasks[executing_thread.id] = task
        task.add_done_callback(
            lambda t: self._on_spawned_thread_done(t, executing_thread)
        )

    def hold_new_threads(self, reason: str = "manual") -> None:
        """Start every thread from now on paused: a pause is in force."""
        self._new_threads_held = True
        self._new_threads_hold_reason = reason

    def release_new_threads(self) -> None:
        """Let threads start running again: the pause was lifted."""
        self._new_threads_held = False
        self._new_threads_hold_reason = "manual"

    def _hold_if_held(self, executing_thread: ExecutingLabwareThread) -> None:
        if self._new_threads_held:
            executing_thread.hold_at_start(reason=self._new_threads_hold_reason)

    def _on_spawned_thread_done(
        self, task: asyncio.Task, thread: ExecutingLabwareThread,
    ) -> None:
        """Done-callback paired with :meth:`start_thread`.

        A crash reaching here escaped the thread's own error handling, so
        nothing paused for a recovery decision and nothing will move this
        thread's labware again. The rest of the run is not torn down over it:
        threads that do not depend on this one have work to finish, and the
        rollup reports the run FAILED once they do. What the operator gets
        instead is an incident naming the labware and where it stopped, which is
        what clearing the deck by hand needs. Cancellation is a normal terminal
        state.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        orca_logger.exception(
            "Spawned thread task raised: %r - task=%s", exc, task,
        )
        self._declare_thread_death(thread, exc)

    def _declare_thread_death(
        self, thread: ExecutingLabwareThread, exc: BaseException,
    ) -> None:
        """Record where a crashed thread left its labware. Silent skip without a
        declarer (fixtures that build workflows directly), and never propagates:
        a recording failure must not become a second crash on the callback."""
        if self._incident_declarer is None:
            return
        labware = thread.thread_instance.labware
        location = thread.current_location
        try:
            self._incident_declarer.declare_thread_death(
                self._context.execution_id,
                thread.id,
                ThreadDiedContext(
                    thread_name=thread.name,
                    labware_name=labware.name,
                    labware_id=labware.id,
                    last_position_id=(
                        location.position_id if location is not None else None
                    ),
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                ),
            )
        except Exception:
            orca_logger.exception(
                "declare_thread_death failed for thread %s", thread.name,
            )

    async def add_and_start_thread(
        self,
        template: ThreadTemplate,
        labware_instance: LabwareInstance | None = None,
        *,
        run_mode: WorkflowRunMode,
    ) -> ExecutingLabwareThread:
        """Compose thread creation, registration, and start into one call.

        Operator-facing entry point for recovery from AUTO_SPAWN_FAILED and for
        UIs that want to inject a thread into a running workflow. Mirrors the
        auto-spawn path but without a shared method or group/submission
        tagging.

        `labware_instance`: when supplied, the factory attaches this existing
        labware (BarcodeAcquisition semantics). When None, the factory mints a
        fresh instance from the template's pool.
        """
        if self._create_thread_fn is None:
            raise RuntimeError(
                "Cannot spawn thread: ExecutingWorkflow has no create_thread_fn "
                "(factory not wired). This is a construction bug, not an "
                "operator error."
            )
        resolved: ResolvedAcquisition | None = None
        if labware_instance is not None:
            resolved = ResolvedAcquisition(labware_instance=labware_instance)
        thread_instance = await self._create_thread_fn(
            template, None, resolved, run_mode,
        )
        executing_thread = self.add_thread(thread_instance)
        self.start_thread(executing_thread, honor_pause_hold=False)
        return executing_thread

    async def wait_all_threads(self) -> None:
        """Wait for every thread to finish, including ones they spawn in turn.

        A submission already inside its injection window still counts as work
        this run owns: its threads are built across an await and are not in the
        list yet. Concluding without them would finish the run and then let them
        run outside it, where nothing they do reaches the event bus.
        """
        seen: set[str] = set()
        while True:
            new = [t for t in self._spawned_threads if t.id not in seen]
            if not new:
                if self._pending_injections > 0:
                    await self._injections_settled.wait()
                    continue
                # No await between this and the latch: a submission cannot open
                # an injection window on a run that has decided it is done.
                self._accepting_injections = False
                break
            seen.update(t.id for t in new)
            await asyncio.gather(*[t.completed.wait() for t in new])

    @property
    def is_accepting_injections(self) -> bool:
        """Whether a joining submission still has a run to join.

        Read before the submit path first awaits, so it cannot rely on
        `wait_all_threads` having woken up to notice: a run whose threads have
        all reached terminal is over whether or not the coroutine has run again
        yet. A submission already inside its injection window holds the run open
        for the threads it is about to add.
        """
        if not self._accepting_injections:
            return False
        if self._pending_injections > 0:
            return True
        threads = self.threads
        return not threads or any(not t.has_completed() for t in threads)

    async def stop_all_thread_tasks(self) -> None:
        """Hard-cancel every thread task this workflow scheduled and drain them.

        Abortive: ``stop_execution`` calls this so spawned co-threads are
        cancelled, not just the execution task. ``execution.task.cancel()``
        only reaches entry threads (they sit in the gather); dynamically
        spawned co-threads are detached and would otherwise survive, leak
        reservations, and re-drive stuck shared actions.

        Uses the same dynamic seen-loop as ``wait_all_threads`` because
        auto-spawn / JOIN_EXISTING batching can create threads mid-run and
        even during this drain: a one-shot snapshot would miss late spawns.
        Each cancelled thread's ``start()`` finally releases its reservations.
        """
        self._tearing_down = True
        coordinator = self._thread_reservation_coordinator
        seen: set[str] = set()
        while True:
            pending = [
                (tid, task) for tid, task in self._thread_tasks.items()
                if tid not in seen and not task.done()
            ]
            if not pending:
                break
            seen.update(tid for tid, _ in pending)
            # Mark dead before cancelling: the system-wide tick loop keeps running
            # and must not grant a request for a thread we are about to kill.
            coordinator.mark_threads_dead({tid for tid, _ in pending})
            for _, task in pending:
                task.cancel()
            await asyncio.gather(
                *(task for _, task in pending), return_exceptions=True,
            )
        # Free reservations a cancelled thread never bound (e.g. parked awaiting
        # the grant), and drop its stale deadlock carry.
        thread_ids = {t.id for t in self.threads}
        coordinator.release_reservations_for_threads(thread_ids)
        coordinator.forget_threads(thread_ids)

    def stop_tick_loop(self) -> None:
        """Stop the tick loop task started by this workflow."""
        try:
            if self._tick_loop_task is not None and not self._tick_loop_task.done():
                self._tick_loop_task.cancel()
        except RuntimeError:
            pass  # Event loop already closed during shutdown
        self._thread_reservation_coordinator.stop_tick_loop()

    async def await_tick_loop_stopped(self) -> None:
        """Wait for the cancelled tick loop to finish unwinding.

        ``stop_tick_loop`` asks it to stop; without this the loop ends only
        because something later in shutdown happens to give it a turn.
        """
        if self._tick_loop_task is None:
            return
        await asyncio.gather(self._tick_loop_task, return_exceptions=True)

    def cleanup_parked_threads(self) -> None:
        """Stop all parked threads so they can finalize, and warn on any
        undelivered items stranded in slot.pending or slot.queue.

        Undelivered items indicate under-fill: contributors were expected
        but never arrived, or a receiver exited early leaving queued items.
        """
        self._tearing_down = True
        if self._labware_registry is None:
            return
        for registered in self._labware_registry.all_parked():
            if registered.thread is not None:
                registered.thread.stop()
        for slot_key, slot in self._labware_registry.all_slots().items():
            abandoned = len(slot.pending) + slot.queue.qsize()
            if abandoned <= 0:
                continue
            orca_logger.warning(
                "Workflow shutdown: dropping %d undelivered method(s) for slot '%s' "
                "(pending=%d, queue=%d). Indicates under-fill.",
                abandoned, slot_key, len(slot.pending), slot.queue.qsize(),
            )
            context = WorkflowExecutionContext(
                execution_id=self._workflow.id, workflow_name=self._workflow.name,
            )
            self._event_bus.emit(f"SLOT.{slot_key}.ABANDONED", context)

    def pause(self) -> None:
        raise NotImplementedError

    def resume(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError
    

class ExecutingWorkflowFactory:
    def __init__(self,
                system_thread_manager: ThreadManager,
                thread_reservation_coordinator: IThreadReservationCoordinator,
                event_bus: IEventBus,
                move_handler: MoveHandler,
                status_manager: StatusManager,
                system_map: SystemMap,
                create_thread_fn: CreateThreadFn | None = None,
                labware_store: ILabwareStore | None = None,
                system: _LabwareAdder | None = None,
                ) -> None:
        self._thread_reservation_coordinator = thread_reservation_coordinator
        self._system_thread_manager = system_thread_manager
        self._event_bus = event_bus
        self._move_handler = move_handler
        self._status_manager = status_manager
        self._system_map = system_map
        self._create_thread_fn = create_thread_fn
        self._labware_store = labware_store
        self._system = system

    def set_runtime_refs(
        self, labware_store: ILabwareStore, system: _LabwareAdder,
    ) -> None:
        """Inject labware_store + system after construction.

        SdkToSystemBuilder builds the factory BEFORE the SystemRuntime
        exists, so these refs are wired in post-hoc when SystemRuntime is
        constructed. Reuse-bind threads need both to materialize fresh
        labware into the dual stores (system.labwares + ILabwareStore).
        """
        self._labware_store = labware_store
        self._system = system

    def create_instance(self, workflow: WorkflowInstance) -> ExecutingWorkflow:
        from orca.runtime.group_aware_labware_registry import GroupAwareLabwareRegistry
        return ExecutingWorkflow(workflow,
                                self._thread_reservation_coordinator,
                                self._system_thread_manager,
                                self._event_bus,
                                self._move_handler,
                                self._status_manager,
                                self._system_map,
                                create_thread_fn=self._create_thread_fn,
                                labware_registry=GroupAwareLabwareRegistry(),
                                labware_store=self._labware_store,
                                system=self._system)
    
class IExecutingWorkflowRegistry(ABC):

    @abstractmethod
    def get_executing_workflow(self, execution_id: str) -> ExecutingWorkflow:
        raise NotImplementedError

    def set_runtime_refs(
        self, labware_store: ILabwareStore, system: _LabwareAdder,
    ) -> None:
        """Inject the runtime's labware_store + system refs into the
        underlying factory so reuse-bind threads can dual-register fresh
        labware (system.labwares + labware_store) when their auto-spawn
        fires.

        Default no-op so subclasses that don't use reuse-bind (System's
        composition-style adapters) keep working without implementing it.
        """
        return None


class ExecutingWorkflowRegistry(IExecutingWorkflowRegistry):
    def __init__(self, workflow_registry: WorkflowRegistry,  factory: ExecutingWorkflowFactory) -> None:
        self._workflow_registry = workflow_registry
        self._factory = factory
        self._executing_registry: Dict[str, ExecutingWorkflow] = {}

    def set_runtime_refs(
        self, labware_store: ILabwareStore, system: _LabwareAdder,
    ) -> None:
        self._factory.set_runtime_refs(labware_store, system)

    def get_executing_workflow(self, execution_id: str) -> ExecutingWorkflow:
        if execution_id in self._executing_registry.keys():
            return self._executing_registry[execution_id]
        else:
            workflow_instance = self._workflow_registry.get_workflow(execution_id)
            executing_workflow = self._factory.create_instance(workflow_instance)
            # execution_id and workflow_instance.id are the same UUID by
            # construction (see WorkflowInstance.__init__); assert it so any
            # future divergence surfaces loudly instead of silently creating
            # duplicate keys in this registry.
            assert executing_workflow.id == execution_id, (
                f"executing_workflow.id ({executing_workflow.id}) must match "
                f"execution_id ({execution_id})"
            )
            self._executing_registry[execution_id] = executing_workflow
            executing_workflow.status = WorkflowStatus.CREATED
            return executing_workflow


class WorkflowThreadManager:
    def __init__(self, system_thread_manager: IThreadManager ) -> None:
        self._system_thread_manager: IThreadManager = system_thread_manager
        self._entry_threads: Dict[str, ExecutingLabwareThread] = {}
        self._workflow_threads: Dict[str, ExecutingLabwareThread] = {}

    @property
    def entry_threads(self) -> List[ExecutingLabwareThread]:
        return list(self._entry_threads.values())

    @property
    def threads(self) -> List[ExecutingLabwareThread]:
        return list(self._workflow_threads.values())

    def add_thread(self, thread: ExecutingLabwareThread, is_entry_thread: bool) -> None:
        if is_entry_thread:
            self._entry_threads[thread.id] = thread
        self._workflow_threads[thread.id] = thread

    async def start_entry_threads(self) -> None:
        await asyncio.gather(*[thread.start() for thread in self.entry_threads])
    