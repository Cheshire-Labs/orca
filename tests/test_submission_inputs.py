"""A run's variables are checked when it is submitted, before anything moves."""

from dataclasses import replace
from pathlib import Path

import pytest

from orca.runtime.profile_store import FileDeploymentProfileStore
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.workflow import WorkflowTemplate
from orca.variables import DeploymentProfile, SubmissionInputsRefusedError, VariableDefinition
from orca.variables.submission_inputs import SubmittedInputs, refusals_for
from orca.runtime.deployment_profile_service import DeploymentProfileService
from tests.memory_activity import memory_runtime
from tests.test_helpers import execution_outcome
from tests.test_system_runtime import _build_simple_system

_SPEED = VariableDefinition(type="int", default=100, min=0, max=2000)
_REQUIRED = VariableDefinition(type="str")
_HAS_SPEED = SubmittedInputs(workflow_name="wf", workflow_definitions={"speed": _SPEED})
_NEEDS_SAMPLE = SubmittedInputs(workflow_name="wf", workflow_definitions={"sample_id": _REQUIRED})


def test_values_the_workflow_accepts_are_not_refused() -> None:
    assert refusals_for(replace(_HAS_SPEED, inline={"speed": 500})) == []


def test_a_name_the_workflow_does_not_define_is_refused_and_the_defined_names_are_listed() -> None:
    assert refusals_for(replace(_HAS_SPEED, inline={"sped": 500})) == [
        "sped is not a variable of workflow wf. Defined: speed.",
    ]


def test_a_value_of_the_wrong_type_is_refused() -> None:
    assert refusals_for(replace(_HAS_SPEED, inline={"speed": "fast"})) == [
        "Variable 'speed' = 'fast': expected type int, got str",
    ]


def test_a_value_out_of_range_is_refused() -> None:
    assert refusals_for(replace(_HAS_SPEED, inline={"speed": 5000})) == [
        "Variable 'speed' = 5000: above maximum 2000.0",
    ]


def test_a_variable_with_no_value_and_no_default_is_refused() -> None:
    assert refusals_for(_NEEDS_SAMPLE) == ["sample_id has no value and no default. Pass it in variables."]


def test_a_standalone_method_need_not_supply_what_it_may_not_read() -> None:
    assert refusals_for(replace(_NEEDS_SAMPLE, every_variable_is_read=False)) == []


def test_a_profile_can_supply_a_required_variable() -> None:
    assert refusals_for(replace(_NEEDS_SAMPLE, profile={"sample_id": "a"})) == []


def test_a_joined_execution_can_supply_a_required_variable() -> None:
    assert refusals_for(replace(_NEEDS_SAMPLE, set_on_joined_execution=frozenset({"sample_id"}))) == []


def test_a_profile_may_hold_names_this_workflow_does_not_use_but_its_values_are_checked() -> None:
    assert refusals_for(replace(_HAS_SPEED, profile={"other_workflows_name": 1})) == []
    assert len(refusals_for(replace(_HAS_SPEED, profile={"speed": 5000}))) == 1


def test_a_global_name_needs_a_global_definition_or_a_global_value() -> None:
    inline = replace(_HAS_SPEED, inline={"global.retries": 1})
    assert len(refusals_for(inline)) == 1
    assert refusals_for(replace(inline, global_values=frozenset({"retries"}))) == []


def test_a_global_value_is_checked_against_its_definition() -> None:
    too_many = replace(
        _HAS_SPEED,
        inline={"global.retries": 99},
        global_definitions={"retries": VariableDefinition(type="int", max=10)},
    )
    assert len(refusals_for(too_many)) == 1


def test_a_computed_name_can_be_overridden() -> None:
    assert refusals_for(replace(_HAS_SPEED, inline={"volume": 5}, computed=frozenset({"volume"}))) == []


def test_every_problem_is_reported_at_once() -> None:
    inputs = SubmittedInputs(
        workflow_name="wf",
        workflow_definitions={"speed": _SPEED, "sample_id": _REQUIRED},
        inline={"speed": "fast", "sped": 1},
    )
    assert len(refusals_for(inputs)) == 3


async def _runtime_with_speed(
    profile_store: FileDeploymentProfileStore | None = None,
) -> tuple[SystemRuntime, WorkflowTemplate]:
    system, workflow = await _build_simple_system()
    system.variable_store.register_workflow_definitions(workflow.name, {"speed": _SPEED})
    runtime = await memory_runtime(
        system, profile_store=DeploymentProfileService(profile_store) if profile_store is not None else None,
    )
    await runtime.start()
    return runtime, workflow


@pytest.mark.asyncio
async def test_a_refused_submission_starts_no_execution() -> None:
    runtime, workflow = await _runtime_with_speed()
    try:
        with pytest.raises(SubmissionInputsRefusedError, match="above maximum"):
            await runtime.submit(workflow, variables={"speed": 5000}, mode=WorkflowRunMode.CHECK_RUN)
        assert runtime.list_executions() == []
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_saved_profile_named_on_a_submit_is_applied(tmp_path: Path) -> None:
    profiles = FileDeploymentProfileStore(tmp_path)
    await profiles.add(DeploymentProfile(name="fast", variables={"speed": 1500}))
    runtime, workflow = await _runtime_with_speed(profiles)
    try:
        submission = await runtime.submit(
            workflow, deployment_profile="fast", mode=WorkflowRunMode.CHECK_RUN,
        )
        assert runtime.system.variable_store.resolve("speed", submission.execution_id) == 1500
        status = await execution_outcome(runtime, submission, timeout=30.0)
        assert status.status == "completed"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_profile_nobody_saved_is_refused_before_an_execution_exists() -> None:
    runtime, workflow = await _runtime_with_speed()
    try:
        with pytest.raises(KeyError, match="ghost"):
            await runtime.submit(workflow, deployment_profile="ghost", mode=WorkflowRunMode.CHECK_RUN)
        assert runtime.list_executions() == []
    finally:
        await runtime.shutdown()
