"""A LIVE execution and a DEVICE_SIM execution must not run at the same time.

Both dispatch over the wire to the same orca-client device connections,
so a DEVICE_SIM run would switch a device to its sim backend on the device
bridge while a LIVE run drives the same device for real (or vice versa),
knocking the live device offline. PURE_SIM never reaches the wire, so it is
exempt and may run alongside anything.

Scope tested:
- LIVE alive + DEVICE_SIM submit            -> REFUSED
- direction symmetry + exemptions, via the `_find_live_sim_conflict` helper
- LIVE alive + PURE_SIM submit              -> ALLOWED (PURE_SIM exempt)
- LIVE finished, then DEVICE_SIM submit     -> ALLOWED (blocker not alive)
"""

import pytest

from orca.runtime.registries import NullGatewayRegistry
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.runtime_interface import ConcurrentLiveSimRefusedError
from orca.runtime.system_runtime import SystemRuntime
from tests.runtime.manual_place_fixtures import (
    _build_manual_place_system,
    _live_connection_source,
)


# -- Class-shape tests -----------------------------------------------------


class TestConcurrentLiveSimRefusedErrorShape:
    def test_carries_typed_fields(self) -> None:
        exc = ConcurrentLiveSimRefusedError(
            blocking_execution_id="exec-abc",
            blocking_workflow_name="wf_one",
            existing_run_mode=WorkflowRunMode.LIVE,
            submitted_run_mode=WorkflowRunMode.DEVICE_SIM,
        )
        assert exc.blocking_execution_id == "exec-abc"
        assert exc.blocking_workflow_name == "wf_one"
        assert exc.existing_run_mode is WorkflowRunMode.LIVE
        assert exc.submitted_run_mode is WorkflowRunMode.DEVICE_SIM

    def test_inherits_runtime_error(self) -> None:
        exc = ConcurrentLiveSimRefusedError(
            blocking_execution_id="x",
            blocking_workflow_name="w",
            existing_run_mode=WorkflowRunMode.DEVICE_SIM,
            submitted_run_mode=WorkflowRunMode.LIVE,
        )
        assert isinstance(exc, RuntimeError)

    def test_message_mentions_identifiers_and_modes(self) -> None:
        exc = ConcurrentLiveSimRefusedError(
            blocking_execution_id="exec-abc",
            blocking_workflow_name="wf_one",
            existing_run_mode=WorkflowRunMode.LIVE,
            submitted_run_mode=WorkflowRunMode.DEVICE_SIM,
        )
        msg = str(exc)
        assert "exec-abc" in msg
        assert "wf_one" in msg
        assert "LIVE" in msg
        assert "DEVICE_SIM" in msg


# -- Runtime-gate behavior --------------------------------------------------


async def test_device_sim_while_live_alive_refused() -> None:
    """With a LIVE execution alive, a DEVICE_SIM submission is refused and
    the envelope names the blocking execution and both run modes."""
    system = await _build_manual_place_system("wf_one")
    runtime = SystemRuntime(
        system,
        gateway_registry=NullGatewayRegistry(),
        connection_source=_live_connection_source(),
    )
    await runtime.start()
    try:
        first = await runtime.submit_workflow("wf_one", mode=WorkflowRunMode.LIVE)
        assert not runtime._executions[first.id].task.done()
        with pytest.raises(ConcurrentLiveSimRefusedError) as exc_info:
            await runtime.submit_workflow("wf_one", mode=WorkflowRunMode.DEVICE_SIM)
        err = exc_info.value
        assert err.blocking_execution_id == first.id
        assert err.blocking_workflow_name == "wf_one"
        assert err.existing_run_mode is WorkflowRunMode.LIVE
        assert err.submitted_run_mode is WorkflowRunMode.DEVICE_SIM
    finally:
        await runtime.shutdown()


async def test_find_live_sim_conflict_directions_and_exemptions() -> None:
    """The helper submit() calls: with a LIVE execution alive, DEVICE_SIM
    conflicts (either direction is symmetric), while a second LIVE (same
    mode) and PURE_SIM (no wire) do not."""
    system = await _build_manual_place_system("wf_dir")
    runtime = SystemRuntime(
        system,
        gateway_registry=NullGatewayRegistry(),
        connection_source=_live_connection_source(),
    )
    await runtime.start()
    try:
        live = await runtime.submit_workflow("wf_dir", mode=WorkflowRunMode.LIVE)
        blocker = runtime._find_live_sim_conflict(WorkflowRunMode.DEVICE_SIM)
        assert blocker is not None
        assert blocker.id == live.id
        # Same wire-mode is allowed; PURE_SIM is exempt.
        assert runtime._find_live_sim_conflict(WorkflowRunMode.LIVE) is None
        assert runtime._find_live_sim_conflict(WorkflowRunMode.PURE_SIM) is None
    finally:
        await runtime.shutdown()


async def test_pure_sim_while_live_alive_allowed() -> None:
    """PURE_SIM never reaches the wire, so it is exempt: a PURE_SIM
    submission is accepted while a LIVE execution is alive."""
    system = await _build_manual_place_system("wf_pure")
    runtime = SystemRuntime(
        system,
        gateway_registry=NullGatewayRegistry(),
        connection_source=_live_connection_source(),
    )
    await runtime.start()
    try:
        await runtime.submit_workflow("wf_pure", mode=WorkflowRunMode.LIVE)
        record = await runtime.submit_workflow(
            "wf_pure", mode=WorkflowRunMode.PURE_SIM,
        )
        assert record.workflow_name == "wf_pure"
    finally:
        await runtime.shutdown()


async def test_conflict_released_after_first_terminates() -> None:
    """Once the LIVE execution's task is done, the gate releases and a
    DEVICE_SIM submission is admitted."""
    system = await _build_manual_place_system("wf_release")
    runtime = SystemRuntime(
        system,
        gateway_registry=NullGatewayRegistry(),
        connection_source=_live_connection_source(),
    )
    await runtime.start()
    try:
        first = await runtime.submit_workflow(
            "wf_release", mode=WorkflowRunMode.LIVE,
        )
        first_execution = runtime._executions[first.id]
        first_execution.task.cancel()
        try:
            await first_execution.task
        except BaseException:
            pass
        assert first_execution.task.done()
        assert runtime._find_live_sim_conflict(WorkflowRunMode.DEVICE_SIM) is None
        record = await runtime.submit_workflow(
            "wf_release", mode=WorkflowRunMode.DEVICE_SIM,
        )
        assert record.id != first.id
    finally:
        await runtime.shutdown()
