"""WorkflowTemplate.add_thread rejects reuse threads as entries.

A `wf.start(thread)` thread is constructed eagerly per submission; if a
reuse-binding thread were registered that way every submission would
mint a fresh LabwareThreadInstance against the same bound labware --
multi-receiver, one-labware regression. Reuse threads must go through
`wf.thread()` so the auto-spawn slot machinery gives one receiver per
slot.
"""

from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.runtime.runtime_interface import ReuseThreadCannotBeEntryError
from orca.spawn import REUSE_EXISTING
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import ThreadFunc, ThreadTemplate
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.test_helpers import create_test_plate_template


@orca.method
async def _trivial_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
    del ctx
    return
    yield


def _trivial_func() -> ThreadFunc:
    async def fn(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield _trivial_method
    return fn


class TestWorkflowReuseThreadCheck:

    def test_wf_start_with_reuse_existing_raises(self) -> None:
        plate = create_test_plate_template("plate_x")
        thread = ThreadTemplate(
            labware_template=plate,
            start=("pad_1", REUSE_EXISTING),
            end="pad_1",
            func=_trivial_func(),
        )
        wf = WorkflowTemplate("wf")
        with pytest.raises(ReuseThreadCannotBeEntryError) as exc:
            wf.add_thread(thread, is_start=True)
        assert exc.value.thread_name == thread.name

    def test_wf_thread_with_reuse_existing_succeeds(self) -> None:
        plate = create_test_plate_template("plate_x")
        thread = ThreadTemplate(
            labware_template=plate,
            start=("pad_1", REUSE_EXISTING),
            end="pad_1",
            func=_trivial_func(),
        )
        wf = WorkflowTemplate("wf")
        wf.add_thread(thread, is_start=False)  # auto-spawn registration path
        assert thread in wf.thread_templates

    def test_wf_start_with_non_reuse_thread_succeeds(self) -> None:
        plate = create_test_plate_template("plate_x")
        thread = ThreadTemplate(
            labware_template=plate, start="pad_1", end="pad_1",
            func=_trivial_func(),
        )
        wf = WorkflowTemplate("wf")
        wf.add_thread(thread, is_start=True)
        assert thread in wf.entry_thread_templates
