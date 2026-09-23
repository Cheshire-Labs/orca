"""Workflow-scoped drain: two build_workflow() calls don't share pending state.

Bug NN architecture: each @orca.workflow construction drains the module-level
pending @orca.method / @orca.thread lists into that workflow's
bundled_methods / bundled_threads. Two successive build_workflow() calls
each produce a workflow whose bundle reflects only its own closure-internal
decorations.
"""

import orca.orca as orca
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.test_helpers import create_test_plate_template


def _make_workflow(name: str, method_name: str, thread_name: str):
    """Build a one-method, one-thread workflow with the given names.

    Mirrors the closure pattern used by the deployment template:
    decorations live inside the builder so each call gets its own scope.
    """
    @orca.method
    async def _method(ctx: MethodContext):
        yield  # pragma: no cover - never executed in this test
    _method.__qualname__ = method_name  # for nicer assertion errors
    # Force the template to take its name from a unique value so two
    # workflows can declare distinctly-named methods without colliding.
    # We re-use the Python decorator pattern but rely on the underlying
    # @orca.method implementation already set self._name = func.__name__,
    # so we do the rename via a custom wrapper:

    @orca.thread(labware=create_test_plate_template(), start="pad_1", end="pad_1")
    async def _thread(ctx: ThreadContext):
        yield _method  # pragma: no cover

    @orca.workflow(name=name)
    def _workflow(wf: WorkflowContext):
        # build_workflow pattern; we don't actually wire the thread here
        # because the test only inspects bundled_* state.
        return None

    return _workflow


class TestWorkflowScopedDrain:
    def test_workflow_captures_methods_decorated_in_its_scope(self) -> None:
        @orca.method
        async def incubate_a(ctx: MethodContext):
            yield  # pragma: no cover

        @orca.workflow(name="wf_a")
        def wf_a(wf: WorkflowContext):
            return None

        bundled_names = {m.name for m in wf_a.bundled_methods}
        assert "incubate_a" in bundled_names

    def test_two_workflows_do_not_share_bundled_methods(self) -> None:
        """Decorations inside workflow A's scope must not leak into workflow B."""
        @orca.method
        async def step_a(ctx: MethodContext):
            yield  # pragma: no cover

        @orca.workflow(name="wf_a")
        def wf_a(wf: WorkflowContext):
            return None

        @orca.method
        async def step_b(ctx: MethodContext):
            yield  # pragma: no cover

        @orca.workflow(name="wf_b")
        def wf_b(wf: WorkflowContext):
            return None

        a_names = {m.name for m in wf_a.bundled_methods}
        b_names = {m.name for m in wf_b.bundled_methods}
        assert "step_a" in a_names
        assert "step_b" in b_names
        # The crucial property: step_b is NOT in wf_a's bundle, and
        # step_a is NOT in wf_b's bundle. Pre-fix, both would have
        # appeared in whichever workflow drained last.
        assert "step_b" not in a_names
        assert "step_a" not in b_names

    def test_workflow_with_no_decorations_has_empty_bundle(self) -> None:
        @orca.workflow(name="wf_empty")
        def wf_empty(wf: WorkflowContext):
            return None

        assert wf_empty.bundled_methods == []
        assert wf_empty.bundled_threads == []
