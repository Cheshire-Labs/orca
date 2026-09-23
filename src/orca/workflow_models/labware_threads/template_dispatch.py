"""Free-helper dispatch for non-``IMethodTemplate`` template shapes
(currently only ``ActionTemplate``). Keeps ``ActionTemplate`` from
having to import ``IThreadContext`` and avoids the otherwise circular
imports.
"""
from collections.abc import AsyncIterator

from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.labware_threads.i_thread_context import IThreadContext
from orca.workflow_models.method import ExecutingMethod, MethodInstance
from orca.workflow_models.workflows.workflow_factories import MethodActionFactory


async def schedule_action_template(
    action_template: ActionTemplate,
    ctx: IThreadContext,
) -> AsyncIterator[ExecutingMethod]:
    """Wrap a bare ``ActionTemplate`` in a synthetic single-action
    ``ExecutingMethod`` so the thread loop sees only methods.
    """
    method_inst = MethodInstance(action_template.operation_name)
    method_inst.append_action(MethodActionFactory(action_template).create_instance())
    ctx.bind_method(method_inst)
    ctx.add_method(method_inst)
    yield ctx.create_executing_method(method_inst)
