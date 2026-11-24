"""A system runs in one run mode at a time, and knows which mode it is in."""

import pytest

from tests.no_devices import NullGatewayRegistry
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.errors import OtherRunModeActiveError
from orca.runtime.system_runtime import SystemRuntime
from tests.runtime.manual_place_fixtures import (
    _build_manual_place_system,
    _live_connection_source,
)
from tests.memory_activity import memory_runtime


async def _runtime(workflow: str) -> SystemRuntime:
    system = await _build_manual_place_system(workflow)
    runtime = await memory_runtime(
        system,
        gateway_registry=NullGatewayRegistry(),
        connection_source=_live_connection_source(),
    )
    await runtime.start()
    return runtime


async def _end(runtime: SystemRuntime, execution_id: str) -> None:
    task = runtime._executions[execution_id].task
    task.cancel()
    try:
        await task
    except BaseException:
        pass


def test_the_refusal_names_the_running_execution_and_both_modes() -> None:
    exc = OtherRunModeActiveError(
        blocking_execution_id="exec-abc",
        blocking_workflow_name="wf_one",
        existing_run_mode=WorkflowRunMode.LIVE,
        submitted_run_mode=WorkflowRunMode.CHECK_RUN,
    )
    msg = str(exc)
    assert isinstance(exc, RuntimeError)
    assert all(part in msg for part in ("exec-abc", "wf_one", "LIVE", "CHECK_RUN"))


@pytest.mark.parametrize(("running", "submitted"), [
    (WorkflowRunMode.LIVE, WorkflowRunMode.SIM),
    (WorkflowRunMode.LIVE, WorkflowRunMode.CHECK_RUN),
    (WorkflowRunMode.SIM, WorkflowRunMode.CHECK_RUN),
    (WorkflowRunMode.CHECK_RUN, WorkflowRunMode.SIM),
])
async def test_a_run_in_another_mode_is_refused_while_one_is_running(
    running: WorkflowRunMode, submitted: WorkflowRunMode,
) -> None:
    runtime = await _runtime("wf_one")
    try:
        first = await runtime.submit_workflow("wf_one", mode=running)
        with pytest.raises(OtherRunModeActiveError) as exc_info:
            await runtime.submit_workflow("wf_one", mode=submitted)
        err = exc_info.value
        assert (err.blocking_execution_id, err.existing_run_mode, err.submitted_run_mode) == (
            first.id, running, submitted,
        )
    finally:
        await runtime.shutdown()


async def test_a_second_run_in_the_same_mode_is_admitted() -> None:
    runtime = await _runtime("wf_same")
    try:
        first = await runtime.submit_workflow("wf_same", mode=WorkflowRunMode.LIVE)
        second = await runtime.submit_workflow("wf_same", mode=WorkflowRunMode.LIVE)
        assert second.id != first.id
    finally:
        await runtime.shutdown()


async def test_another_mode_is_admitted_once_the_running_execution_ends() -> None:
    runtime = await _runtime("wf_release")
    try:
        first = await runtime.submit_workflow("wf_release", mode=WorkflowRunMode.LIVE)
        await _end(runtime, first.id)
        record = await runtime.submit_workflow("wf_release", mode=WorkflowRunMode.CHECK_RUN)
        assert record.id != first.id
    finally:
        await runtime.shutdown()


async def test_the_system_is_in_no_run_mode_before_its_first_run() -> None:
    runtime = await _runtime("wf_none")
    try:
        assert runtime.system_run_mode() is None
    finally:
        await runtime.shutdown()


async def test_the_system_is_in_the_running_executions_mode_and_stays_in_it_after_it_ends() -> None:
    runtime = await _runtime("wf_mode")
    try:
        first = await runtime.submit_workflow("wf_mode", mode=WorkflowRunMode.SIM)
        assert runtime.system_run_mode() is WorkflowRunMode.SIM
        await _end(runtime, first.id)
        assert runtime.system_run_mode() is WorkflowRunMode.SIM

        await runtime.submit_workflow("wf_mode", mode=WorkflowRunMode.CHECK_RUN)
        assert runtime.system_run_mode() is WorkflowRunMode.CHECK_RUN
    finally:
        await runtime.shutdown()
