"""Synthesize a one-method workflow from labware start/end mappings.

Shared by ``SystemRuntime.submit_method`` (runtime submission path) and
``StandaloneMethodExecutor`` (SDK no-runtime path). The first labware's
thread owns the method (yields it, materializing the shared ExecutingMethod);
the rest register as co-labware threads the method's slot pulls in at
execution. A set of join-only threads has no owner and hangs, so the
owner/contributor split is load-bearing, not cosmetic.

Both execution paths await every thread to completion (the runtime via
``_run_workflow`` -> ``wait_all_threads``, the SDK via
``WorkflowExecutor.start``), so co-labware return legs always finish.
"""

from collections.abc import AsyncGenerator, Sequence

from orca.resource_models.labware import LabwareTemplate
from orca.resource_models.location import Location
from orca.workflow_models.method_template import (
    IMethodTemplate,
    JoinTemplate,
    MethodTemplate,
)
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflow_templates import WorkflowTemplate

# (labware_template, start_location, end_location); order matters -- the
# first entry is the method owner, the rest are co-labware contributors.
StandaloneThreadSpec = tuple[LabwareTemplate, Location, Location]


def build_standalone_method_workflow(
    name: str,
    method_template: MethodTemplate,
    threads: Sequence[StandaloneThreadSpec],
) -> WorkflowTemplate:
    """Build a synthetic single-method workflow over the given labware threads."""
    workflow = WorkflowTemplate(name)
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
