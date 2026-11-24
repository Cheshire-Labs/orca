"""A check run is unpaced: only a sim run carries a speed-up."""

import pytest

from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.errors import SimSpeedupNotApplicableError
from orca.modes.sim_pacing import SimPacer, pacer_for_run_mode
from orca.runtime.system_runtime import SystemRuntime
from tests.memory_activity import memory_runtime
from tests.no_devices import NullGatewayRegistry
from tests.runtime.manual_place_fixtures import _build_manual_place_system, _live_connection_source


async def _runtime(workflow: str) -> SystemRuntime:
    system = await _build_manual_place_system(workflow)
    runtime = await memory_runtime(
        system,
        gateway_registry=NullGatewayRegistry(),
        connection_source=_live_connection_source(),
        default_sim_speedup=10.0,
    )
    await runtime.start()
    return runtime


async def test_a_check_run_has_no_pacer_even_with_a_deployment_default() -> None:
    runtime = await _runtime("wf_check")
    try:
        execution = await runtime.submit_workflow("wf_check", mode=WorkflowRunMode.CHECK_RUN)
        assert runtime.get_execution(execution.id).sim_speedup is None
    finally:
        await runtime.shutdown()


async def test_a_check_run_refuses_a_speed_up_at_submission() -> None:
    runtime = await _runtime("wf_check_speedup")
    try:
        with pytest.raises(SimSpeedupNotApplicableError):
            await runtime.submit_workflow("wf_check_speedup", mode=WorkflowRunMode.CHECK_RUN, sim_speedup=10.0)
    finally:
        await runtime.shutdown()


async def test_a_check_run_refuses_a_speed_up_change() -> None:
    runtime = await _runtime("wf_check_change")
    try:
        execution = await runtime.submit_workflow("wf_check_change", mode=WorkflowRunMode.CHECK_RUN)
        with pytest.raises(SimSpeedupNotApplicableError):
            runtime.set_execution_sim_speedup(execution.id, 10.0)
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("mode", [WorkflowRunMode.CHECK_RUN, WorkflowRunMode.LIVE])
def test_a_check_run_or_live_thread_seeds_no_pacer(mode: WorkflowRunMode) -> None:
    assert pacer_for_run_mode(mode, SimPacer(10.0)) is None


def test_a_sim_thread_seeds_the_runs_pacer() -> None:
    pacer = SimPacer(10.0)
    assert pacer_for_run_mode(WorkflowRunMode.SIM, pacer) is pacer
