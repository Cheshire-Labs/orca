"""MutationCoordinator: skip + insert primitives for runtime method/action mutation.

ALL operations require the target thread to be PAUSED. The coordinator validates
state, resolves workflow context from the thread, and handles the full factory
chain for insert operations.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from orca.system.interfaces import IMethodRegistry, IThreadTemplateRegistry
from orca.system.mutation.errors import (
    ActionAlreadyExecutingError,
    CannotReplaceCurrentMethodError,
    MethodAlreadyInProgressError,
    MethodNotAssignedError,
    MethodNotInProgressError,
    MutationLeavesInputUnassignedError,
    ReplacementSharesTargetNameError,
    ThreadNotPausedError,
)
from orca.resource_models.labware import AnyLabwareTemplate, LabwareTemplate
from orca.system.thread_manager_interface import IThreadManager
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.actions.dynamic_resource_action import UnresolvedLocationAction
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread
from orca.workflow_models.method import ExecutingMethod, MethodInstance
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.mutation_position import AtHead, Before, InsertPosition
from orca.workflow_models.status_enums import LabwareThreadStatus, MethodStatus
from orca.variables.variable_store import NullVariableResolver
from orca.workflow_models.workflows.workflow_factories import MethodActionFactory
from orca.workflow_models.workflows.workflow_registry import IExecutingMethodRegistry


def _input_slot_label(template: LabwareTemplate | AnyLabwareTemplate) -> str:
    """How an unfilled input slot is named to an operator.

    A wildcard's own name is the internal token `$any`, which means nothing to
    the person reading the refusal.
    """
    return "any labware" if template.is_wildcard else template.name


class MutationCoordinator:

    def __init__(
        self,
        method_registry: IMethodRegistry,
        executing_method_registry: IExecutingMethodRegistry,
        thread_template_registry: IThreadTemplateRegistry,
        thread_manager: IThreadManager,
    ) -> None:
        self._method_registry = method_registry
        self._executing_method_registry = executing_method_registry
        self._thread_template_registry = thread_template_registry
        self._thread_manager = thread_manager

    # --- Thread lookup and validation ---

    def _get_thread(self, thread_id: str) -> ExecutingLabwareThread:
        for t in self._thread_manager.executing_threads:
            if t.id == thread_id:
                return t
        raise ValueError(f"No executing thread with id '{thread_id}'")

    def _validate_paused(self, thread: ExecutingLabwareThread) -> None:
        status = thread.status
        if status != LabwareThreadStatus.PAUSED:
            raise ThreadNotPausedError(
                thread_name=thread.name, current_status=status.name,
            )

    # _find_pending_method and _find_pending_action removed: with generator-based
    # lanes, we cannot enumerate future items. Use skip-by-name via add_skip instead.

    # --- Method mutation: skip + insert + abort ---

    def _find_in_progress_method(self, name: str) -> ExecutingMethod | None:
        """Check if any executing thread has a method with this name IN_PROGRESS."""
        for t in self._thread_manager.executing_threads:
            m = t.assigned_method
            if m is not None and m.name == name and m.status == MethodStatus.IN_PROGRESS:
                return m
        return None

    def skip_method(
        self,
        thread_id: str,
        method_id: str | None = None,
        method_name: str | None = None,
    ) -> None:
        if method_id is None and method_name is None:
            raise ValueError("Must provide method_id or method_name")
        if method_id is not None and method_name is not None:
            raise ValueError("Provide only one of method_id or method_name")
        thread = self._get_thread(thread_id)
        self._validate_paused(thread)
        # Exactly one is non-None by the validation above. ``or`` between
        # two real Optional[str] values is fine; the cheat would be a
        # literal-string fallback. Assert narrows for the type checker.
        skip_name = method_name or method_id
        assert skip_name is not None
        in_progress = self._find_in_progress_method(skip_name)
        if in_progress is not None:
            raise MethodAlreadyInProgressError(method_name=skip_name)
        thread.method_lane.add_skip(skip_name)

    async def abort_method(
        self,
        thread_id: str,
        method_id: str | None = None,
        method_name: str | None = None,
    ) -> None:
        """Abort an in-progress method: abort running action + skip remaining."""
        if method_id is None and method_name is None:
            raise ValueError("Must provide method_id or method_name")
        thread = self._get_thread(thread_id)
        self._validate_paused(thread)
        method = thread.assigned_method
        if method is None:
            raise ValueError(
                f"Thread {thread.name} has no assigned method to abort."
            )
        target_name = method_name or method_id
        assert target_name is not None
        if method.name != target_name:
            raise ValueError(
                f"Thread {thread.name}'s current method is '{method.name}', "
                f"not '{target_name}'."
            )
        await method.abort()

    async def replace_method(
        self,
        thread_id: str,
        target_name: str,
        template: MethodTemplate,
    ) -> bool:
        """Replace a method. Returns True if the replacement was STAGED for
        recovery, False if it was fully spliced in place.

        - Target is a PENDING method: place ``template`` where ``target_name``
          would run (``Before`` anchor) and skip the target. Fully applied;
          the thread runs it on resume. Returns False.
        - Target is THIS thread's current (assigned) method AND the thread is
          ERROR-paused: the failed method was already consumed off the lane, so
          it cannot be skipped. Stage the replacement to run next (``AtHead``)
          and return True -- the caller must drop the failed method via
          ``recover_thread(ABORT_METHOD)`` to run it. The continue stays
          operator-gated.
        - Target is the current (assigned) method but the thread is MANUALLY
          paused: refused. The current method has left the lane (so skip is a
          no-op) and a manual pause has no recovery decision to stage against;
          ``CannotReplaceCurrentMethodError`` points the operator at
          abort_method + insert.

        Matching is by name and first-match (``Before`` anchors + skip are
        one-shot on first consumption); names are not unique on the lane, so
        the first occurrence is the one replaced. On the pending path the
        replacement must have a different name than ``target_name`` -- a
        same-named substitute would itself be caught by the one-shot skip
        (``ReplacementSharesTargetNameError``).

        The replacement is built BEFORE any lane mutation, so a bad template
        leaves the lane untouched.
        """
        thread = self._get_thread(thread_id)
        self._validate_paused(thread)
        current = thread.assigned_method
        targets_current = current is not None and current.name == target_name
        if targets_current and not thread.is_error_paused:
            raise CannotReplaceCurrentMethodError(method_name=target_name)
        if not targets_current and template.name == target_name:
            raise ReplacementSharesTargetNameError(name=target_name)
        method_instance = await self._create_method_instance_from_template_async(template)
        if targets_current:
            self._finalize_inserted_method(thread, method_instance, AtHead())
            return True
        self._finalize_inserted_method(thread, method_instance, Before(target_name))
        thread.method_lane.add_skip(target_name)
        return False

    def insert_method(
        self,
        thread_id: str,
        template: MethodTemplate,
        where: InsertPosition,
    ) -> None:
        """Synchronous insert. Used by `_SystemMutationContext` callbacks
        (the `mutate_on_next_pause` path), which run synchronously per the
        `IThreadMutationContext` contract. Iterates the template generator
        on a worker thread (`_create_method_instance_from_template`).

        Wire callers (REST/CLI via ThreadFacade.insert_method) MUST use
        `insert_method_async` instead -- this path blocks the event loop
        for up to 10s on the thread-pool fallback.
        """
        thread = self._get_thread(thread_id)
        self._validate_paused(thread)

        method_instance = self._create_method_instance_from_template(template)
        self._finalize_inserted_method(thread, method_instance, where)

    async def insert_method_async(
        self,
        thread_id: str,
        template: MethodTemplate,
        where: InsertPosition,
    ) -> None:
        """Async insert. Iterates the template generator with native
        `async for` on the caller's event loop -- no thread-pool, no
        10s blocking timeout. The wire path (ThreadFacade.insert_method)
        routes here so REST/CLI callers do not stall the daemon.
        """
        thread = self._get_thread(thread_id)
        self._validate_paused(thread)

        method_instance = await self._create_method_instance_from_template_async(template)
        self._finalize_inserted_method(thread, method_instance, where)

    def _finalize_inserted_method(
        self, thread: ExecutingLabwareThread,
        method_instance: MethodInstance, where: InsertPosition,
    ) -> None:
        """Shared post-collection wiring for sync + async insert_method.

        Registers the new method instance, creates the ExecutingMethod,
        binds the thread's labware, and places on the method lane.
        """
        self._method_registry.add_method(method_instance)

        wf_context = thread.context

        executing_method = self._executing_method_registry.create_executing_method(
            method_instance.id, wf_context
        )

        thread_template = self._thread_template_registry.get_labware_thread_template(
            wf_context.workflow_name, thread.template_name
        )
        executing_method.assign_thread(thread_template.labware_template, thread)

        where.apply(thread.method_lane, executing_method)

    def _create_method_instance_from_template(self, template: MethodTemplate) -> MethodInstance:
        """Sync wrapper for the async generator iteration.

        Used by the sync `insert_method` path (mutate_on_next_pause
        callbacks). Async callers should use
        `_create_method_instance_from_template_async` and avoid the
        thread-pool detour.
        """
        method_inst = MethodInstance(template.name, failure_policy=template.method_failure_policy)

        async def _collect_actions() -> list[ActionTemplate]:
            return await self._collect_actions_from_template(template)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, _collect_actions())
            collected = future.result(timeout=10.0)

        self._append_action_instances(method_inst, collected)
        return method_inst

    async def _create_method_instance_from_template_async(
        self, template: MethodTemplate,
    ) -> MethodInstance:
        """Native-async generator iteration on the caller's loop."""
        method_inst = MethodInstance(template.name, failure_policy=template.method_failure_policy)
        collected = await self._collect_actions_from_template(template)
        self._append_action_instances(method_inst, collected)
        return method_inst

    def _append_action_instances(
        self, method_inst: MethodInstance, action_templates: list[ActionTemplate],
    ) -> None:
        """Build action instances from collected templates and append them."""
        for at in action_templates:
            factory = MethodActionFactory(at)
            method_inst.append_action(factory.create_instance())

    async def _collect_actions_from_template(
        self, template: MethodTemplate,
    ) -> list[ActionTemplate]:
        """Iterate the template's async generator and collect ActionTemplates.

        Shared by sync + async insert_method paths. Returns a flat list
        regardless of whether the generator yields singletons or lists.
        """
        dummy_ctx = MethodContext(
            action_queue=asyncio.Queue(),
            assigned_labware={},
            variable_store=NullVariableResolver(),
            execution_id="mutation-insert",
        )
        actions: list[ActionTemplate] = []
        async for item in template.func(dummy_ctx):
            if isinstance(item, list):
                actions.extend(item)
            elif isinstance(item, ActionTemplate):
                actions.append(item)
        return actions

    # --- Action mutation: skip + insert ---

    def skip_action(
        self,
        thread_id: str,
        action_id: str | None = None,
        action_command: str | None = None,
    ) -> None:
        if action_id is None and action_command is None:
            raise ValueError("Must provide action_id or action_command")
        thread = self._get_thread(thread_id)
        self._validate_paused(thread)
        method = thread.assigned_method
        if method is None:
            raise MethodNotAssignedError(thread_name=thread.name)
        skip_name = action_command or action_id
        assert skip_name is not None
        current = method.current_action
        if current is not None and current.command == skip_name:
            raise ActionAlreadyExecutingError(action_name=skip_name)
        method.action_lane.add_skip(skip_name)

    def _get_in_progress_method_for_action_mutation(
        self, thread: ExecutingLabwareThread,
    ) -> ExecutingMethod:
        method = thread.assigned_method
        if method is None:
            raise MethodNotAssignedError(thread_name=thread.name)
        if method.status != MethodStatus.IN_PROGRESS:
            raise MethodNotInProgressError(
                thread_name=thread.name, method_name=method.name,
                current_status=method.status.name,
            )
        return method

    def _build_wired_action(
        self, method: ExecutingMethod, action_template: ActionTemplate,
    ) -> UnresolvedLocationAction:
        """Build an action and wire it against every thread CURRENTLY
        participating in ``method`` (owner plus any joined contributors),
        not just the thread that requested the mutation.

        A shared action's declared inputs span every thread converged on it;
        wiring only the caller leaves a co-thread's slot permanently
        unassigned, which the owner's co-labware gate can never satisfy (it
        requires every declared slot assigned, not just physically present --
        see ``AssignedLabwareManager.all_inputs_assigned``). Refuses instead
        of returning a half-wired action if a declared slot still has no
        matching participant once every current thread has been tried; that
        is the case an unjoined co-thread would otherwise deadlock on.
        """
        action = MethodActionFactory(action_template).create_instance()
        for participant_id in method.participating_thread_ids:
            participant = self._get_thread(participant_id)
            thread_template = self._thread_template_registry.get_labware_thread_template(
                participant.context.workflow_name, participant.template_name
            )
            action.try_assign_labware(thread_template.labware_template, participant.labware)
        unassigned = [
            _input_slot_label(template)
            for template in action.expected_input_templates
            if not action.is_input_assigned(template)
        ]
        if unassigned:
            raise MutationLeavesInputUnassignedError(action.command, unassigned)
        return action

    def insert_action(
        self,
        thread_id: str,
        action_template: ActionTemplate,
        where: InsertPosition,
    ) -> None:
        thread = self._get_thread(thread_id)
        self._validate_paused(thread)
        method = self._get_in_progress_method_for_action_mutation(thread)
        action = self._build_wired_action(method, action_template)
        where.apply(method.action_lane, action)

    def replace_action(
        self,
        thread_id: str,
        target_command: str,
        action_template: ActionTemplate,
    ) -> bool:
        """Replace an action. Returns True if STAGED for recovery, False if
        fully spliced.

        Placement is always AtHead (the action lane keys Before/After anchors
        on ``tag`` but skips on ``command``, so an in-place anchor would
        require the target to be tagged): the replacement runs next, ahead of
        any other pending actions in the method. Matching is one-shot on the
        first pending consumption of ``target_command``.

        - Target is a PENDING action: also skip the named command, so the
          original is dropped when reached. Fully applied. Returns False. The
          replacement must have a different command than ``target_command``
          (else the one-shot skip drops it: ``ReplacementSharesTargetNameError``).
        - Target is the currently-executing action AND the thread is
          ERROR-paused: it was already consumed and cannot be skipped. Stage
          the replacement only and return True -- the caller drops the failed
          action via ``recover_thread(ABORT_ACTION)``.
        - Target is the currently-executing action but the thread is MANUALLY
          paused: refused (``ActionAlreadyExecutingError``). There is no
          recovery decision to stage against; use the error-recovery path.
        """
        thread = self._get_thread(thread_id)
        self._validate_paused(thread)
        method = self._get_in_progress_method_for_action_mutation(thread)
        current = method.current_action
        is_current = current is not None and current.command == target_command
        if is_current and not thread.is_error_paused:
            raise ActionAlreadyExecutingError(action_name=target_command)
        action = self._build_wired_action(method, action_template)
        if not is_current and action.command == target_command:
            raise ReplacementSharesTargetNameError(name=target_command)
        AtHead().apply(method.action_lane, action)
        if not is_current:
            method.action_lane.add_skip(target_command)
        return is_current
