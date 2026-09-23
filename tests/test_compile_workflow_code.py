"""compile_workflow_code: source string -> WorkflowTemplate.

Used by a hosted deployment's submission pipeline to extract a workflow from a posted
source string before writing the file. The function focuses on exec +
extraction; AST validation is the caller's responsibility.

The canonical shape is a top-level ``build_workflow(topology)`` function
whose body decorates @orca.workflow inside a closure. Cold-boot
(deployment template's system.py walks workflows/ and calls
build_workflow on each) and live-submit (this function) converge on
the same import shape.
"""

import pytest

from orca.sdk.build import compile_workflow_code


_VALID_BUNDLED_SOURCE = '''
import orca.orca as orca

def build_workflow(topology):
    @orca.workflow(name="walk8_wf")
    def walk8_wf(wf):
        return None
    return walk8_wf
'''


_NO_BUILD_WORKFLOW_SOURCE = '''
import orca.orca as orca

@orca.workflow(name="flat_only")
def flat_only(wf):
    return None
'''


_BUILDER_RETURNS_NON_TEMPLATE = '''
def build_workflow(topology):
    return 42
'''


class TestCompileWorkflowCode:
    def test_compiles_bundled_workflow(self) -> None:
        template = compile_workflow_code(_VALID_BUNDLED_SOURCE)
        assert template.name == "walk8_wf"

    def test_rejects_source_without_build_workflow(self) -> None:
        """Flat-shape (top-level decorator only) is rejected per the
        single-shape rule: cold-boot calls build_workflow(topology) so
        flat workflows would silently disappear after a reload.
        """
        with pytest.raises(ValueError, match="build_workflow"):
            compile_workflow_code(_NO_BUILD_WORKFLOW_SOURCE)

    def test_rejects_builder_returning_non_template(self) -> None:
        with pytest.raises(ValueError, match="must return a WorkflowTemplate"):
            compile_workflow_code(_BUILDER_RETURNS_NON_TEMPLATE)

    def test_compile_failure_propagates(self) -> None:
        bad_source = "def broken(:\n    pass\n"
        with pytest.raises(SyntaxError):
            compile_workflow_code(bad_source)
