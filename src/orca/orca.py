"""Public SDK surface for orca code-first execution model.

Usage:
    import orca.orca as orca

    @orca.action(device=pool, inputs=[plate_1, tips_96])
    async def run_step(ctx):
        await ctx.device().run_protocol("step.pro", {})

    @orca.method
    async def step(ctx):
        yield run_step

    @orca.thread(labware=plate_1, start=stacker, end=stacker)
    async def plate_journey(ctx):
        yield step

    @orca.thread(labware=tips, start=tip_stacker, end=waste)
    async def tips_journey(ctx):
        yield orca.join()

    @orca.workflow(name="my_assay")
    def workflow(wf):
        wf.start(plate_journey)
        wf.thread(tips_journey)
"""

from typing import Any, Callable, Dict, List, Optional, overload

from orca.resource_models.capacity import (
    CapacityExceededError,
    CapacityPolicy,
    OverflowAction,
    RecoverableCapacityExceededError,
)
from orca.resource_models.devices import Device
from orca.resource_models.labware import AnyLabwareTemplate, LabwareTemplate
from orca.resource_models.labware_state import LabwareSlot
from orca.resource_models.location import Location
from orca.resource_models.resource_pool import ResourcePool
from orca.state.records import DeclaredTracking
from orca.resource_models.well_selector import WellSelector
from orca.variables.variable_definition import VariableDefinition
from orca.workflow_models.action_template import Action, ActionTemplate, ActionFunc
from orca.workflow_models.event_step import BranchStepTemplate, WaitStepTemplate
from orca.workflow_models.method import ExecutingMethod
from orca.workflow_models.method_template import (
    IMethodTemplate,
    JoinTemplate,
    MethodFunc,
    MethodTemplate,
    drain_pending_method_templates,
    register_pending_method,
)
from orca.workflow_models.overflow_strategy import (
    IOverflowStrategy,
    IWorkflowRef,
    RecoverableRejectStrategy,
    RejectStrategy,
    SequentialStashStrategy,
)
from orca.workflow_models.park_template import ParkTemplate
from orca.workflow_models.status_enums import FailurePolicy
from orca.workflow_models.thread_template import (
    EndArg,
    ThreadFunc,
    ThreadTemplate,
    drain_pending_thread_templates,
    register_pending_thread,
)
from orca.sdk.build import SystemBuild, Topology, build_system
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate


@overload
def method(func: MethodFunc) -> MethodTemplate: ...
@overload
def method(*, failure_policy: FailurePolicy) -> Callable[[MethodFunc], MethodTemplate]: ...

def method(
    func: MethodFunc | None = None,
    failure_policy: FailurePolicy | None = None,
) -> MethodTemplate | Callable[[MethodFunc], MethodTemplate]:
    """Decorator that creates a MethodTemplate from an async generator.

    Usage:
        @orca.method                            # bare decorator
        @orca.method(failure_policy=...)        # with args
    """
    def decorator(fn: MethodFunc) -> MethodTemplate:
        template = MethodTemplate(
            name=fn.__name__,
            func=fn,
            failure_policy=failure_policy,
        )
        register_pending_method(template)
        return template
    if func is not None:
        return decorator(func)
    return decorator


def action(
    device: Device[Any] | ResourcePool,
    inputs: List[LabwareTemplate | AnyLabwareTemplate],
    outputs: Optional[List[LabwareTemplate | AnyLabwareTemplate]] = None,
    failure_policy: FailurePolicy | None = None,
    tag: str | None = None,
    deck_positions: Optional[Dict[LabwareTemplate, str]] = None,
    well_selectors: dict[str, WellSelector] | None = None,
    declares: DeclaredTracking | None = None,
) -> Callable[[ActionFunc], Action]:
    """Decorator that creates an Action template from an async function."""
    def decorator(func: ActionFunc) -> Action:
        return Action(
            func=func,
            resource=device,
            inputs=inputs,
            outputs=outputs,
            failure_policy=failure_policy,
            tag=tag,
            deck_positions=deck_positions,
            well_selectors=well_selectors,
            declares=declares,
        )
    return decorator


def on(event_name: str, timeout: float | None = None) -> WaitStepTemplate:
    """Wait for a named event before continuing.

    Latch semantics: sees the latest publish regardless of timing.
    timeout=None (default) waits indefinitely.
    """
    return WaitStepTemplate(event_name=event_name, timeout=timeout)


def join(
    method: MethodTemplate | None = None,
    allows: list[MethodTemplate] | None = None,
) -> JoinTemplate:
    """Declare that this thread joins a shared method from spawn context.

    Usage in a thread generator:
        yield orca.join()                        # join whatever method spawned me
        yield orca.join(some_method)             # join a specific method
        yield orca.join(allows=[m1, m2])         # validated against allowed methods
    """
    return JoinTemplate(method=method, allows=allows)


def park(location: str | list[str]) -> ParkTemplate:
    """Suspend this thread at an author-declared parking spot until the engine wakes it.

    A list declares interchangeable spots (a hotel's pads); all are requested
    at once and the park completes at whichever is granted first. Route score
    drives that choice, not the order you list them (a held corridor position
    can outrank score). Parked labware is never relocated by the engine.

    Usage in a thread generator:
        yield orca.park("hotel_pad_1")
        yield orca.park([f"hotel_pad_{i}" for i in range(1, 13)])
    """
    return ParkTemplate(location=location)


def branch(
    event_name: str,
    branches: dict[str, list[IMethodTemplate]],
    timeout: float | None = None,
) -> BranchStepTemplate:
    """Branch on event value. Use ``"else"`` key for fallback."""
    return BranchStepTemplate(event_name=event_name, branches=branches, timeout=timeout)


def thread(
    labware: LabwareTemplate,
    start: Location | str | tuple[Location | str, str],
    end: EndArg,
    contributes_to: list[str] | None = None,
    required: bool = True,
    immovable: bool = False,
) -> Callable[[ThreadFunc], ThreadTemplate]:
    """Decorator that creates a ThreadTemplate from an async generator.

    The generator receives a ThreadContext and yields methods.
    start/end can be Location objects or string names (resolved by build_system).
    end also accepts a LIST of interchangeable candidate spots (a hotel's
    shelves); the thread ends at whichever is granted, so concurrent
    executions' plates take distinct shelves instead of stacking on one.
    Route score drives which one, not the order you list them.

    contributes_to: the LABWARE TEMPLATE NAME of each receiver this
    thread feeds (the receiver's ``labware=`` template name, NOT the
    receiver's thread-function name). The receiver's slot closes (and its
    ``while ctx.has_more_work()`` loop exits) once no thread that transitively
    feeds it up the ``contributes_to`` chain is still live -- not just its direct
    feeders, so a live upstream thread that will still spawn a feeder holds it open.
    Example (receiver labware template is ``final_plate``)::

        @orca.thread(labware=plate_1, start=..., end=...,
                     contributes_to=["final_plate"])
        async def plate_1_journey(ctx):
            ...
            yield combine_plates  # feeds the `final_plate` receiver

    required (T6h): default True - every submitted LabwareGroup must carry
    a LabwareGroupMember whose thread_template_name matches this thread.
    Set ``required=False`` to allow groups to omit this thread; the thread
    only spawns for groups that explicitly name it. Useful for conditional
    per-group optional threads (e.g., spike-in controls).

    immovable (S3-R1): default False. When True, the engine's deadlock
    detector treats this thread's labware as a terminal blocker: any other
    thread requesting a location currently holding this labware fails fast
    with `UnresolvableDeadlockError` instead of silently retrying. Use for
    deck-resident reagents whose labware will not be moved by any thread.
    Cannot be combined with `wf.start()` -- enforced at build time.
    """
    def decorator(func: ThreadFunc) -> ThreadTemplate:
        template = ThreadTemplate(
            labware_template=labware,
            start=start,
            end=end,
            func=func,
            contributes_to=contributes_to,
            required=required,
            immovable=immovable,
        )
        register_pending_thread(template)
        return template
    return decorator


WorkflowFunc = Callable[[WorkflowContext], None]


def workflow(
    name: str,
) -> Callable[[WorkflowFunc], WorkflowTemplate]:
    """Decorator that creates a WorkflowTemplate from a builder function.

    Usage::

        @orca.workflow(name="smc_assay")
        def smc_assay(wf):
            wf.start(plate_1_journey)
            wf.thread(tips_thread)
            wf.on("THREAD.CREATED", MyHandler())
    """
    def decorator(func: WorkflowFunc) -> WorkflowTemplate:
        import asyncio
        template = WorkflowTemplate(name)
        ctx = WorkflowContext(template)
        result = func(ctx)
        if asyncio.iscoroutine(result):
            raise TypeError(
                f"@orca.workflow function '{func.__name__}' must be a regular function, "
                "not async. Use wf.start(), wf.thread(), wf.on() to declare structure."
            )
        # Workflow-scoped drain: capture every method/thread that was
        # decorated since the previous @orca.workflow construction (or
        # since module import) and attach them to this workflow. Two
        # build_workflow() calls each get their own scope, so a method
        # defined inside workflow A's closure cannot leak into workflow B.
        # Method and thread names are unique per workflow (the registries
        # key by (workflow_name, name)), so two workflows may share a method
        # or thread name; the collision check fires when the workflow is
        # registered, not here.
        template.attach_bundled_templates(
            methods=drain_pending_method_templates(),
            threads=drain_pending_thread_templates(),
        )
        return template
    return decorator
