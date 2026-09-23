"""Shutdown stops the work before it closes what the work is using.

A workflow's entry threads run in tasks the execution task does not own. A
shutdown that cancelled only the execution task left them running, then closed
the stores under them: the next move died on a closed database, the thread
paused for an operator who was never coming, and nothing could end it. In CI
that surfaced as a test timing out in teardown while the loop waited on a task
that would never finish; in a deployment it is a process that will not exit.
"""
import asyncio

import pytest

from orca.gateway.controller.controller import DeviceController
from orca.runtime.registries import NullGatewayRegistry
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime import system_runtime
from orca.runtime.system_runtime import SystemRuntime
from tests.runtime.test_deck_layout_required_on_submit import _build_system_with_lh


async def _submitted_then_shut_down() -> list[asyncio.Task[object]]:
    """Submit a workflow, shut down at once, and report what is still running."""
    system = await _build_system_with_lh(deck_layout=None)
    runtime = SystemRuntime(system, gateway_registry=NullGatewayRegistry())
    await runtime.start()
    try:
        await runtime.submit_workflow(
            "deck_layout_test_wf", mode=WorkflowRunMode.PURE_SIM,
        )
    finally:
        await runtime.shutdown()
    # Read the tasks the moment shutdown returns, with nothing in between. A
    # shutdown that waited for its work has nothing left running here; one that
    # did not shows its orphans without anybody having to wait for them.
    here = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if t is not here and not t.done()]


@pytest.mark.timeout(60)
async def test_shutdown_leaves_no_task_running() -> None:
    """The submit returns while the entry threads are still starting, which is
    the window a shutdown used to walk into."""
    still_running = await _submitted_then_shut_down()

    assert still_running == [], (
        "shutdown returned with work still running against stores it then "
        "closed: " + ", ".join(t.get_name() for t in still_running)
    )


@pytest.mark.timeout(60)
async def test_shutdown_gives_up_on_a_drain_that_will_not_finish(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The drain waits, and a wait in shutdown has to end. A thread that never
    comes back must cost a warning, not a process that will not exit."""
    async def never_returns(self: SystemRuntime) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(system_runtime, "_SHUTDOWN_DRAIN_TIMEOUT_S", 0.1)
    monkeypatch.setattr(
        SystemRuntime, "_abort_and_await_executions", never_returns,
    )
    system = await _build_system_with_lh(deck_layout=None)
    runtime = SystemRuntime(system, gateway_registry=NullGatewayRegistry())
    await runtime.start()

    with caplog.at_level("WARNING"):
        await runtime.shutdown()

    assert "running executions" in caplog.text, (
        "shutdown gave up without saying what it stopped waiting for"
    )



@pytest.mark.timeout(60)
async def test_shutdown_waits_for_a_cancel_still_going_out(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Aborting a device command tells the device bridge to drop it, on a task
    the unwinding caller schedules and never awaits. Shutdown is the only thing
    that can wait for it, and a wait in shutdown has to end."""
    async def never_returns(self: DeviceController) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(system_runtime, "_SHUTDOWN_DRAIN_TIMEOUT_S", 0.1)
    monkeypatch.setattr(DeviceController, "await_cancels_sent", never_returns)
    system = await _build_system_with_lh(deck_layout=None)
    runtime = SystemRuntime(system, gateway_registry=NullGatewayRegistry())
    await runtime.start()

    with caplog.at_level("WARNING"):
        await runtime.shutdown()

    assert "cancels" in caplog.text, (
        "shutdown never waited for the cancels it scheduled"
    )
