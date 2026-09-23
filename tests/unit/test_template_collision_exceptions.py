"""Pin the typed exception envelope for thread/method template name collisions.

The typed fields let API surfaces produce a typed envelope (e.g. a hosted deployment's
`thread_template_name_collision` HTTP code) instead of regex-matching the
message. Thread names are unique deployment-wide; method names are unique
per workflow, so a method collision only fires when two distinct
method objects share a name inside ONE workflow.

Backward compatibility: both classes inherit from `KeyError`, so any
caller that catches `KeyError` continues to handle the case.
"""

from orca.workflow_models.method_template import MethodTemplateNameCollisionError
from orca.workflow_models.thread_template import ThreadTemplateNameCollisionError


class TestThreadTemplateNameCollisionError:
    def test_carries_template_name(self) -> None:
        exc = ThreadTemplateNameCollisionError("sample_plate")
        assert exc.template_name == "sample_plate"

    def test_carries_optional_workflow_attribution(self) -> None:
        exc = ThreadTemplateNameCollisionError(
            "sample_plate",
            conflicting_workflow="hamilton_smc_assay",
            existing_workflow="lab_sim_starter",
        )
        assert exc.template_name == "sample_plate"
        assert exc.conflicting_workflow == "hamilton_smc_assay"
        assert exc.existing_workflow == "lab_sim_starter"

    def test_default_message_includes_template_name(self) -> None:
        exc = ThreadTemplateNameCollisionError("sample_plate")
        assert "sample_plate" in str(exc)
        assert "Thread template name collision" in str(exc)

    def test_caller_supplied_message_overrides_default(self) -> None:
        exc = ThreadTemplateNameCollisionError(
            "sample_plate", message="custom message wins",
        )
        assert "custom message wins" in str(exc)

    def test_inherits_from_key_error_for_backward_compat(self) -> None:
        """Old call sites catching KeyError keep handling the case."""
        exc = ThreadTemplateNameCollisionError("sample_plate")
        assert isinstance(exc, KeyError)


class TestMethodTemplateNameCollisionError:
    def test_carries_template_name(self) -> None:
        exc = MethodTemplateNameCollisionError("incubate")
        assert exc.template_name == "incubate"

    def test_inherits_from_key_error(self) -> None:
        exc = MethodTemplateNameCollisionError("incubate")
        assert isinstance(exc, KeyError)

    def test_default_message_includes_template_name(self) -> None:
        exc = MethodTemplateNameCollisionError("incubate")
        assert "incubate" in str(exc)
        assert "Method template name collision" in str(exc)
