"""A cooperative stop must end a thread parked on an error, and must not cut
short a rendezvous the thread is still driving.

An error pause is the longest wait in the system: it ends when a person decides.
The manual pause races the stop event so an operator can call a held thread off;
the error pause watches only the recovery channel, so a thread parked on a failed
dispense answered nothing short of a task cancellation.

Stopping the owner of a shared action was worse than doing nothing. The stop
published a terminal outcome to the rendezvous the owner was still inside, and
every contributor then re-read that finished outcome on a method that never
advances -- a loop with no await in it, which starves the event loop flat and
takes the whole runtime down with it.
"""

import asyncio

import pytest

from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)
from tests.test_deck_resident_reagent_pipetting import build_pipetting_bench
from tests.test_helpers import wait_for_paused_thread, wait_for_runtime_condition

_TERMINAL = {"COMPLETED", "ABORTED", "STOPPED", "FAILED"}


class _DispenseFails(RecordingLiquidHandlerDriver):
    """Picks tips and aspirates for real, then dies at the dispense."""

    async def dispense(self, request):  # noqa: ANN001 - mirrors the driver signature
        raise RuntimeError("Simulated dispense failure")


class _DispenseNeverReturns(RecordingLiquidHandlerDriver):
    """Holds the action open so the stop lands with an action still bound."""

    def __init__(self, inner: ChatterboxLiquidHandlerDriver) -> None:
        super().__init__(inner)
        self.reached_dispense = asyncio.Event()

    async def dispense(self, request):  # noqa: ANN001 - mirrors the driver signature
        self.reached_dispense.set()
        await asyncio.Event().wait()


async def _started(driver: RecordingLiquidHandlerDriver) -> tuple[SystemRuntime, str]:
    build, _ = await build_pipetting_bench(driver)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    record = await runtime.submit_workflow(
        "pipetting_wf", mode=WorkflowRunMode.PURE_SIM,
    )
    return runtime, record.id


def _owner(runtime: SystemRuntime, execution_id: str) -> ExecutingLabwareThread:
    """The thread that yields the shared method; the reservoir and tip rack
    threads join it as contributors."""
    return next(
        t for t in runtime._get_execution_threads(execution_id)
        if t.name.startswith("sample_plate")
    )


@pytest.mark.slow
@pytest.mark.asyncio
async def test_a_stop_ends_a_thread_parked_on_a_failed_action() -> None:
    driver = _DispenseFails(ChatterboxLiquidHandlerDriver(num_channels=8))
    runtime, execution_id = await _started(driver)
    try:
        await wait_for_paused_thread(runtime, execution_id, timeout=30.0)
        thread = _owner(runtime, execution_id)

        thread.stop()

        await wait_for_runtime_condition(
            runtime,
            lambda: thread.status.name in _TERMINAL,
            timeout=20.0,
            message=f"the stopped thread stayed {thread.status.name}",
        )
        assert thread.status.name == "ABORTED", (
            "the pause let go through the recovery channel, which ends a thread "
            "at ABORTED"
        )
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_a_stop_mid_action_leaves_the_runtime_answering() -> None:
    """The stop is recorded and the contributors keep following the live action.

    Any await after the stop is the assertion: a starved event loop never gets
    back to this coroutine, so the read below is what proves the runtime is
    still running at all.
    """
    driver = _DispenseNeverReturns(ChatterboxLiquidHandlerDriver(num_channels=8))
    runtime, execution_id = await _started(driver)
    try:
        await asyncio.wait_for(driver.reached_dispense.wait(), timeout=30.0)
        thread = _owner(runtime, execution_id)

        thread.stop()

        await asyncio.wait_for(runtime.labware.list_all(), timeout=20.0)
        assert thread.status.name == "STOPPING"
        contributors = [
            t for t in runtime._get_execution_threads(execution_id)
            if t is not thread
        ]
        assert contributors, "the bench must have joined the reservoir and tips"
        assert all(t.status.name not in _TERMINAL for t in contributors), (
            "the stop resolved a rendezvous its owner was still driving"
        )
    finally:
        await runtime.shutdown()
