from collections.abc import AsyncGenerator, AsyncIterator
from typing import Callable, List, Optional
from orca.events.event_channel import EventChannelRegistry
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.labware_threads.i_thread_context import IThreadContext
from orca.workflow_models.interfaces import IMethod
from orca.workflow_models.method import ExecutingMethod, MethodInstance
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.status_enums import FailurePolicy

from abc import ABC, abstractmethod

MethodFunc = Callable[[MethodContext], AsyncGenerator[ActionTemplate, None]]


class IMethodTemplate(ABC):
    """Common interface for anything yielded by a thread or workflow as a
    method-level step. Concrete forms include ``MethodTemplate`` (the user
    decorator path), ``JoinTemplate`` (multi-labware join), ``WaitStepTemplate``
    and ``BranchStepTemplate`` (event-step sentinels), and ``ParkTemplate``.

    Every implementation must expose a stable ``name`` so the registry,
    event bus, and method-completion bookkeeping can identify the step.
    Promoting this to the interface removes the ``isinstance`` /
    ``getattr`` dance previously needed by callers that walk
    ``WorkflowTemplate.bundled_methods``.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-visible name; unique within a workflow's method set."""
        ...

    @abstractmethod
    def schedule(
        self,
        ctx: IThreadContext,
        registry: EventChannelRegistry,
    ) -> AsyncIterator[ExecutingMethod]:
        """Drive whatever side-effects this template owns (moves, queue
        reads, event waits) and yield zero or more ``ExecutingMethod``
        instances for the thread loop to run.
        """
        ...


class MethodTemplateNameCollisionError(KeyError):
    """Two distinct ``MethodTemplate`` objects share the same name within one workflow.

    Method names are unique per workflow, not deployment-wide: two
    workflows may each declare a ``combine_plates`` method. The collision
    fires only when two distinct method objects claim the same name inside
    the same workflow's bundle. Inherits ``KeyError`` for backward compat.
    """

    def __init__(
        self,
        template_name: str,
        *,
        conflicting_workflow: str | None = None,
        existing_workflow: str | None = None,
        message: str | None = None,
    ) -> None:
        self.template_name = template_name
        self.conflicting_workflow = conflicting_workflow
        self.existing_workflow = existing_workflow
        if message is None:
            message = (
                f"Method template name collision: '{template_name}' "
                "already registered. Each method name must be unique "
                "within the workflow."
            )
        super().__init__(message)


class MethodTemplate(IMethodTemplate):
    """Template for a named method that yields ActionTemplates via a generator.

    Created via @orca.method decorator or MethodTemplate(name, func=...). Only
    the decorator path registers the template for catalog discovery; direct
    construction produces a template object without side effects.
    """
    def __init__(
        self,
        name: str,
        func: MethodFunc,
        failure_policy: Optional[FailurePolicy] = None,
    ):
        self._name = name
        self._method_failure_policy: FailurePolicy = failure_policy or FailurePolicy.PAUSE
        self._func = func
        self._injected_source: str | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def func(self) -> MethodFunc:
        return self._func

    @property
    def injected_source(self) -> str | None:
        """The source `compile_method_code` compiled this from, or None for
        a method authored in a workflow file. Set post-construction --
        `compile_method_code` does not call this class's constructor
        directly, the injected source's own `@orca.method` decoration does.
        """
        return self._injected_source

    @injected_source.setter
    def injected_source(self, value: str) -> None:
        self._injected_source = value

    def to_dict(self) -> dict[str, str | None]:
        """JSON-safe self-projection for the `@dangerous` audit trail.

        Without this, `_to_json_safe` falls back to a bare repr, and an
        injected method's actual source -- the thing that ran against real
        hardware -- is unrecoverable from the audit log afterwards.
        """
        return {"name": self.name, "injected_source": self._injected_source}

    @property
    def method_failure_policy(self) -> FailurePolicy:
        return self._method_failure_policy

    async def schedule(
        self,
        ctx: IThreadContext,
        registry: EventChannelRegistry,
    ) -> AsyncIterator[ExecutingMethod]:
        import asyncio as _asyncio

        if ctx.register_method_template is not None:
            try:
                ctx.register_method_template(self)
            except KeyError:
                pass

        method_inst = MethodInstance(self.name, failure_policy=self.method_failure_policy)
        method_ctx = MethodContext(
            action_queue=_asyncio.Queue(),
            assigned_labware={},
            variable_store=ctx.variable_store,
            execution_id=ctx.execution_context.execution_id,
            event_channel_registry=registry,
            submission_id=ctx.submission_id,
            event_emitter=ctx.event_emitter,
            workflow_name=ctx.execution_context.workflow_name,
            thread_id=ctx.thread_id,
        )
        await populate_method_actions(self, method_inst, method_ctx)
        ctx.bind_method(method_inst)
        ctx.add_method(method_inst)
        yield ctx.create_executing_method(method_inst)


_PENDING_METHOD_TEMPLATES: List[MethodTemplate] = []


def drain_pending_method_templates() -> List[MethodTemplate]:
    """Return and clear the list of MethodTemplates awaiting catalog registration.

    Populated exclusively by ``@orca.method``, which calls ``register_pending_method``
    after constructing the template. Direct construction (``MethodTemplate(...)``)
    does NOT append to this list -- in that case the template is expected to
    either (a) be yielded from a running thread, at which point the execution
    engine registers it on first encounter, or (b) be handed to a builder
    explicitly. ``SdkToSystemBuilder`` drains this list when building the
    template registry so decorator-authored methods are catalog-visible
    before any workflow runs.
    """
    out = list(_PENDING_METHOD_TEMPLATES)
    _PENDING_METHOD_TEMPLATES.clear()
    return out


def register_pending_method(template: MethodTemplate) -> None:
    """Queue a method template for catalog registration. Called by ``@orca.method``."""
    _PENDING_METHOD_TEMPLATES.append(template)


async def iter_action_templates(
    template: MethodTemplate,
    ctx: MethodContext,
) -> AsyncIterator[ActionTemplate]:
    """Iterate the action templates yielded by a method's generator.

    Normalizes the yield protocol (single item or list-of-items) and
    filters to ``ActionTemplate`` only -- non-action yields
    (``MethodTemplate``, ``JoinTemplate``, ``WaitStepTemplate``,
    ``BranchStepTemplate``, ``ParkTemplate``) are skipped.

    Pre-validation paths (e.g. a hosted deployment's labware-compatibility dry-run
    behind insert_method) only need the action shape; the runtime
    engine uses the full ``_make_yield_adapter`` strategy in
    ``executing_labware_thread.py`` for live execution.

    The yield-protocol ``isinstance`` lives at the user-generator API
    boundary where yields are typed ``object``. Callers see only
    ``ActionTemplate`` and never branch on type themselves.
    """
    async for item in template.func(ctx):
        emitted = item if isinstance(item, list) else [item]
        for entry in emitted:
            if isinstance(entry, ActionTemplate):
                yield entry


async def populate_method_actions(
    template: "MethodTemplate",
    method: IMethod,
    ctx: MethodContext,
) -> None:
    """Append each action the template's generator yields to `method`.

    Single source for materializing a method's actions: used both by the
    live thread path (MethodTemplate.schedule) and by StandaloneMethodExecutor,
    which pre-builds the shared executing method before any thread runs.
    """
    from orca.workflow_models.workflows.workflow_factories import MethodActionFactory
    async for action_item in template.func(ctx):
        action_templates = action_item if isinstance(action_item, list) else [action_item]
        for at in action_templates:
            if not isinstance(at, ActionTemplate):
                raise TypeError(
                    f"Method '{template.name}' yielded {type(at).__name__}, "
                    f"expected ActionTemplate"
                )
            method.append_action(MethodActionFactory(at).create_instance())


class JoinTemplate(IMethodTemplate):
    """Declares that this thread joins a shared method from spawn context.

    Replaces SharedMethodTemplate with type-safe references. Three usage modes:
    - JoinTemplate(): join whatever method triggered the auto-spawn
    - JoinTemplate(method=m): join a specific method
    - JoinTemplate(allows=[m1, m2]): join from spawn context, validated against list
    """
    def __init__(
        self,
        method: Optional[MethodTemplate] = None,
        allows: Optional[List[MethodTemplate]] = None,
        as_owner: bool = False,
    ) -> None:
        self._method = method
        self._allows = allows
        self._as_owner = as_owner

    @property
    def name(self) -> str:
        if self._method is not None:
            return f"join:{self._method.name}"
        return "join:auto"

    @property
    def method(self) -> Optional[MethodTemplate]:
        return self._method

    @property
    def allows(self) -> Optional[List[MethodTemplate]]:
        return self._allows

    def validate_spawn_context(self, method_name: str) -> None:
        if self._method is not None and self._method.name != method_name:
            raise ValueError(
                f"Thread spawned for method '{method_name}' but orca.join() "
                f"expected: '{self._method.name}'"
            )
        if self._allows is not None:
            allowed_names = [m.name for m in self._allows]
            if method_name not in allowed_names:
                raise ValueError(
                    f"Thread spawned for method '{method_name}' but orca.join() "
                    f"only allows: {allowed_names}"
                )

    async def schedule(
        self,
        ctx: IThreadContext,
        registry: EventChannelRegistry,
    ) -> AsyncIterator[ExecutingMethod]:
        # orca.join() crosses a method boundary; release any prior
        # iteration's holdover before waiting on the next.
        ctx.release_holdover()

        method_to_join: ExecutingMethod | None = None

        slot = ctx.my_slot()
        if slot is not None:
            method_to_join = await slot.await_next_method(ctx.stop_event)
            if method_to_join is None:
                return

        if method_to_join is None:
            shared = ctx.shared_executing_method
            if shared is None:
                raise ValueError(
                    "Thread yielded orca.join() but no method available. "
                    "Ensure this thread is registered with wf.thread() "
                    "and the parent method includes this thread's "
                    "labware in its action inputs."
                )
            method_to_join = shared

        ctx.bind_method(method_to_join)
        self.validate_spawn_context(method_to_join.name)

        # An owner join drives the method directly; a shared method with only
        # contributors and no owner has nothing to resolve its actions and hangs.
        if self._as_owner:
            yield method_to_join
            return

        method_to_join.shared_coord.add_contributor(ctx.thread_id)
        try:
            yield method_to_join
        finally:
            method_to_join.shared_coord.remove_contributor(ctx.thread_id)


