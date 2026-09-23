"""An operator can see and clear the submission values that outrank their edits.

A variable supplied on a submission lands in that submission's partition, which
wins over the per-execution partition every operator surface writes to. Without
a way to read or clear the winning layer, an edit mid-run reports success, reads
back the operator's own value, and the run keeps resolving the submission value.
"""

import pytest

from orca.variables import VariableDefinition, VariableService, VariableStore
from orca.variables.resolution import VariableSource


def _service_with_one_execution() -> VariableService:
    service = VariableService(VariableStore())
    service.register_workflow_definitions(
        "wf", {"inject_fault": VariableDefinition(type="bool", default=False)},
    )
    service.create_execution("exec-1", "wf")
    return service


class TestSubmissionValuesOutrankExecutionWrites:

    def test_an_execution_scope_write_does_not_reach_a_submission_thread(self) -> None:
        service = _service_with_one_execution()
        service.set_submission("inject_fault", True, "exec-1", "sub-a")

        service.set("inject_fault", False, "exec-1")

        assert service.resolve("inject_fault", "exec-1", submission_id="sub-a") is True

    def test_clearing_the_submission_value_lets_the_execution_write_through(self) -> None:
        service = _service_with_one_execution()
        service.set_submission("inject_fault", True, "exec-1", "sub-a")
        service.set("inject_fault", False, "exec-1")

        service.unset_submission("inject_fault", "exec-1", "sub-a")

        assert service.resolve("inject_fault", "exec-1", submission_id="sub-a") is False

    def test_clearing_one_submission_leaves_its_siblings_alone(self) -> None:
        service = _service_with_one_execution()
        service.set_submission("inject_fault", True, "exec-1", "sub-a")
        service.set_submission("inject_fault", True, "exec-1", "sub-b")

        service.unset_submission("inject_fault", "exec-1", "sub-a")

        assert service.resolve("inject_fault", "exec-1", submission_id="sub-a") is False
        assert service.resolve("inject_fault", "exec-1", submission_id="sub-b") is True

    def test_clearing_a_value_no_submission_holds_is_not_an_error(self) -> None:
        service = _service_with_one_execution()
        service.unset_submission("inject_fault", "exec-1", "sub-a")
        assert service.has_submission_value("inject_fault", "exec-1", "sub-a") is False

    def test_clearing_on_an_unknown_execution_is_refused(self) -> None:
        service = _service_with_one_execution()
        with pytest.raises(KeyError):
            service.unset_submission("inject_fault", "nope", "sub-a")

    def test_has_submission_value_reports_partition_membership_not_resolvability(
        self,
    ) -> None:
        """The workflow default resolves for every submission, but no submission
        partition holds it, so nothing would be popped by a clear."""
        service = _service_with_one_execution()
        assert service.resolve("inject_fault", "exec-1", submission_id="sub-a") is False
        assert service.has_submission_value("inject_fault", "exec-1", "sub-a") is False


class TestExplainTellsTheOperatorWhichValueWins:

    def test_it_names_every_submission_that_shadows_the_execution_value(self) -> None:
        service = _service_with_one_execution()
        service.set("inject_fault", False, "exec-1")
        service.set_submission("inject_fault", True, "exec-1", "sub-a")
        service.set_submission("inject_fault", True, "exec-1", "sub-b")

        resolution = service.explain("inject_fault", "exec-1")

        assert resolution.shadowed is True
        assert [o.submission_id for o in resolution.overrides] == ["sub-a", "sub-b"]

    def test_it_reports_the_value_a_submission_without_an_override_resolves(
        self,
    ) -> None:
        service = _service_with_one_execution()
        service.set("inject_fault", True, "exec-1")

        resolution = service.explain("inject_fault", "exec-1")

        assert resolution.value is True
        assert resolution.source is VariableSource.EXECUTION
        assert resolution.overrides == ()
        assert resolution.shadowed is False

    def test_it_gives_each_submission_its_own_value_when_they_disagree(self) -> None:
        service = VariableService(VariableStore())
        service.register_workflow_definitions(
            "wf", {"shake_time": VariableDefinition(type="int", default=60)},
        )
        service.create_execution("exec-1", "wf")
        service.set_submission("shake_time", 120, "exec-1", "sub-a")
        service.set_submission("shake_time", 30, "exec-1", "sub-b")

        resolution = service.explain("shake_time", "exec-1")

        assert {(o.submission_id, o.value) for o in resolution.overrides} == {
            ("sub-a", 120), ("sub-b", 30),
        }
        assert resolution.value == 60
        assert resolution.source is VariableSource.WORKFLOW_DEFAULT

    def test_it_reports_a_global_value_as_coming_from_the_global_layer(self) -> None:
        service = VariableService(VariableStore())
        service.register_workflow_definitions("wf", {})
        service.create_execution("exec-1", "wf")
        service.set_global("bench_id", "A")

        resolution = service.explain("global.bench_id", "exec-1")

        assert resolution.value == "A"
        assert resolution.source is VariableSource.GLOBAL

    def test_a_name_no_layer_holds_resolves_to_nothing_rather_than_raising(
        self,
    ) -> None:
        """An operator asking about a name that resolves nowhere still deserves
        the submission breakdown, so explain reports the absence instead."""
        service = _service_with_one_execution()
        service.set_submission("only_here", 5, "exec-1", "sub-a")

        resolution = service.explain("only_here", "exec-1")

        assert resolution.value is None
        assert resolution.source is None
        assert [(o.submission_id, o.value) for o in resolution.overrides] == [
            ("sub-a", 5),
        ]

    def test_it_refuses_an_unknown_execution(self) -> None:
        service = _service_with_one_execution()
        with pytest.raises(KeyError):
            service.explain("inject_fault", "nope")


class TestTheMergedListingAdmitsWhenSubmissionsDisagree:

    def test_it_flags_a_name_two_submissions_hold_different_values_for(self) -> None:
        """The merged view has one slot per name, so two submissions that
        disagree cannot both be shown; the source says so rather than
        presenting one of them as the answer."""
        service = VariableService(VariableStore())
        service.register_workflow_definitions(
            "wf", {"shake_time": VariableDefinition(type="int", default=60)},
        )
        service.create_execution("exec-1", "wf")
        service.set_submission("shake_time", 120, "exec-1", "sub-a")
        service.set_submission("shake_time", 30, "exec-1", "sub-b")

        source = service.get_all_with_source("exec-1")["shake_time"][1]

        assert source is VariableSource.SUBMISSION_DIVERGED

    def test_it_does_not_flag_submissions_that_agree(self) -> None:
        service = VariableService(VariableStore())
        service.register_workflow_definitions(
            "wf", {"shake_time": VariableDefinition(type="int", default=60)},
        )
        service.create_execution("exec-1", "wf")
        service.set_submission("shake_time", 120, "exec-1", "sub-a")
        service.set_submission("shake_time", 120, "exec-1", "sub-b")

        value, source = service.get_all_with_source("exec-1")["shake_time"]

        assert (value, source) == (120, VariableSource.SUBMISSION)


class TestResolveStaysTheSingleSourceOfPrecedence:

    def test_the_source_a_resolve_reports_matches_the_value_it_returns(self) -> None:
        """Reading the value and reading where it came from must not be two
        implementations of the precedence order that can drift apart."""
        service = _service_with_one_execution()
        service.set_global("inject_fault", True)
        service.set("inject_fault", False, "exec-1")
        service.set_submission("inject_fault", True, "exec-1", "sub-a")

        resolution = service.explain("inject_fault", "exec-1")

        for submission_id, expected_source in (
            ("sub-a", VariableSource.SUBMISSION),
            ("sub-b", VariableSource.EXECUTION),
        ):
            binding = resolution.for_submission(submission_id)
            assert binding is not None
            assert binding.source is expected_source
            assert binding.value == service.resolve(
                "inject_fault", "exec-1", submission_id=submission_id,
            )

    def test_a_submission_gets_nothing_when_no_layer_holds_the_name(self) -> None:
        service = _service_with_one_execution()
        resolution = service.explain("nothing_defines_me", "exec-1")
        assert resolution.for_submission("sub-a") is None


class TestASubmitTimeValueIsReachableOnTheRunningExecution:

    async def test_variables_passed_to_submit_land_in_the_submission_partition(
        self,
    ) -> None:
        """The bench case: a value supplied on the submit is what the threads
        resolve, and the operator can find it under that submission's id."""
        import asyncio

        from orca.runtime.run_modes import WorkflowRunMode
        from orca.runtime.system_runtime import SystemRuntime
        from tests.test_system_runtime import _build_simple_system

        system, workflow = await _build_simple_system()
        runtime = SystemRuntime(system)
        await runtime.start()
        try:
            submission = await runtime.submit(
                workflow,
                variables={"inject_fault": True},
                mode=WorkflowRunMode.PURE_SIM,
            )
            execution = runtime._executions[submission.execution_id]
            # The runtime writes the submission partition before it publishes
            # the workflow, so this wait is the write barrier. explain() is
            # sync, so nothing runs between it and the wait returning.
            await asyncio.wait_for(execution.workflow_attached.wait(), timeout=10.0)

            resolution = runtime.variables.explain(
                "inject_fault", submission.execution_id,
            )

            assert [(o.submission_id, o.value) for o in resolution.overrides] == [
                (submission.id, True),
            ]
            assert resolution.shadowed is True
        finally:
            await runtime.shutdown(confirm=True)
