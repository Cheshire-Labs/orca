"""Synthesize a one-method workflow from labware start/end mappings.

Built by ``SystemRuntime.submit_method``. The first labware's
thread owns the method (yields it, materializing the shared ExecutingMethod);
the rest register as co-labware threads the method's slot pulls in at
execution. A set of join-only threads has no owner and hangs, so the
owner/contributor split is load-bearing, not cosmetic.

The runtime awaits every thread to completion (``_run_workflow`` ->
``wait_all_threads``), so co-labware return legs always finish.
"""

from collections.abc import AsyncGenerator, Sequence

from orca.resource_models.labware import LabwareTemplate
from orca.resource_models.location import Location
from orca.workflow_models.method_template import (
    IMethodTemplate,
    JoinTemplate,
    MethodTemplate,
)
from orca.spawn import LEAVE_IN_PLACE, REUSE_EXISTING
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import EndArg, StartArg, ThreadTemplate
from orca.workflow_models.workflow_templates import EventHookInfo, WorkflowTemplate

# (labware_template, start_location, end_location); order matters -- the
# first entry is the method owner, the rest are co-labware contributors.
StandaloneThreadSpec = tuple[LabwareTemplate, StartArg, EndArg]


def with_the_workflows_kinds(
    workflow: WorkflowTemplate, labware: LabwareTemplate, start: Location, end: Location,
) -> StandaloneThreadSpec:
    """``start`` and ``end`` with the start and end kinds the workflow's own thread for ``labware`` declares.

    A deck-resident plate its workflow reuses and leaves in place stays on the deck after a standalone run.
    """
    own = next((t for t in workflow.thread_templates if t.labware_template.name == labware.name), None)
    if own is None:
        return labware, start, end
    start_arg: StartArg = (start, REUSE_EXISTING) if own.start_reuse_existing else start
    end_arg: EndArg = (end, LEAVE_IN_PLACE) if own.end_leave_in_place else end
    return labware, start_arg, end_arg


def starts_from_reuse(spec: StandaloneThreadSpec) -> bool:
    """A labware the run reuses where it stands, which cannot own the method: its thread cannot be the entry."""
    start = spec[1]
    return isinstance(start, tuple) and start[1] == REUSE_EXISTING


def build_standalone_method_workflow(
    name: str,
    method_template: MethodTemplate,
    threads: Sequence[StandaloneThreadSpec],
    event_hooks: Sequence[EventHookInfo] = (),
) -> WorkflowTemplate:
    """Build a synthetic single-method workflow over the given labware threads, with `event_hooks` attached."""
    workflow = WorkflowTemplate(name)
    for hook in event_hooks:
        workflow.add_event_handler(hook.event_name, hook.handler)
    for idx, (labware, start, end) in enumerate(threads):
        yielded: IMethodTemplate = (
            method_template if idx == 0
            else JoinTemplate(method=method_template)
        )

        async def _thread_func(
            ctx: ThreadContext, y: IMethodTemplate = yielded,
        ) -> AsyncGenerator[IMethodTemplate, None]:
            del ctx
            yield y

        thread = ThreadTemplate(labware, start, end, func=_thread_func)
        if idx == 0:
            workflow.add_thread(thread, is_start=True)
        else:
            workflow.add_thread(thread, is_start=False)
            workflow.register_auto_spawn(thread)
    return workflow
