"""While a run is active, running or paused, an operator's or the assistant's command goes in the run's mode, not the device's own setting."""

import asyncio
import contextvars
from orca.operations.device_registry import registry_view
from unittest.mock import AsyncMock, MagicMock

from pydantic import JsonValue

from tests.gateway._operator_command import run_operator_command
from orca.resource_models.resource_pool import ResourcePool
from orca.modes.device_modes import DeviceMode
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.system_runtime import SystemRuntime
from tests.gateway_parts import gateway_with
from tests.runtime.test_an_offline_live_device_refuses_a_run_at_its_start import _device, _runtime, _until


async def _mode_an_operator_command_is_sent_in(runtime: ISystemRuntime, device_name: str) -> WorkflowRunMode:
    tracker, controller = MagicMock(), MagicMock()
    snapshot = MagicMock()
    snapshot.interfaces = ["IShaker"]
    tracker.get_device = AsyncMock(return_value=snapshot)
    sent: list[WorkflowRunMode] = []

    async def execute_command(**kwargs: JsonValue | WorkflowRunMode) -> None:
        mode = kwargs["effective_mode"]
        assert isinstance(mode, WorkflowRunMode)
        sent.append(mode)

    controller.execute_command = AsyncMock(side_effect=execute_command)
    # A request handler runs with no run in force; the test suite seeds one for every test.
    await contextvars.Context().run(asyncio.ensure_future, run_operator_command(
        gateway_with(tracker=tracker, controller=controller), runtime, device_name,
        "shake", {"speed": 500.0, "duration": 1.0},
    ))
    return sent[0]


async def _a_sim_run_beside_an_idle_live_device(prefix: str) -> tuple[SystemRuntime, str, asyncio.Event]:
    """A sim run holds one shaker mid-shake; a second shaker, set to live, sits idle beside it."""
    shaker, driver = _device(f"{prefix}_a", powered=True, setting=DeviceMode.LIVE)
    idle, _ = _device(f"{prefix}_b", powered=True, setting=DeviceMode.LIVE)
    driver.hold = asyncio.Event()
    runtime, workflow = await _runtime(
        prefix, [shaker], ["pad1"], more_pools=[ResourcePool("idle", [idle])], connected=[shaker.name, idle.name],
    )
    record = await runtime.submit_workflow(workflow, mode=WorkflowRunMode.SIM)
    await _until(lambda: driver.shakes == 1)
    return runtime, record.id, driver.hold


async def test_a_command_to_a_live_device_during_a_paused_sim_run_is_simulated() -> None:
    runtime, execution_id, hold = await _a_sim_run_beside_an_idle_live_device("paused_sim")
    try:
        runtime.pause_execution(execution_id)

        assert await _mode_an_operator_command_is_sent_in(runtime, "paused_sim_b") is WorkflowRunMode.SIM
    finally:
        hold.set()
        await runtime.shutdown()


async def test_a_command_to_a_live_device_during_a_running_sim_run_is_simulated() -> None:
    """A system runs in one mode at a time: a sim run that is still running keeps a takeover off hardware too."""
    runtime, _execution_id, hold = await _a_sim_run_beside_an_idle_live_device("running_sim")
    try:
        assert await _mode_an_operator_command_is_sent_in(runtime, "running_sim_b") is WorkflowRunMode.SIM
    finally:
        hold.set()
        await runtime.shutdown()


async def test_with_no_active_run_the_device_uses_its_own_setting() -> None:
    runtime, _execution_id, hold = await _a_sim_run_beside_an_idle_live_device("finished_sim")
    hold.set()
    try:
        await _until(lambda: runtime.active_run_mode() is None)

        assert await _mode_an_operator_command_is_sent_in(runtime, "finished_sim_b") is WorkflowRunMode.LIVE
    finally:
        await runtime.shutdown()


async def test_an_operator_command_during_a_sim_run_waits_at_the_runs_speedup() -> None:
    runtime, execution_id, hold = await _a_sim_run_beside_an_idle_live_device("paced_sim")
    try:
        runtime.set_execution_sim_speedup(execution_id, 5.0)

        assert runtime.operator_sim_pacer().speedup == 5.0
    finally:
        hold.set()
        await runtime.shutdown()


async def test_the_device_list_shows_the_world_an_operator_command_would_go_to() -> None:
    """During a sim run a live device's commands go to sim, so its row says SIM, not its own setting."""
    runtime, _execution_id, hold = await _a_sim_run_beside_an_idle_live_device("listed_sim")
    try:
        rows = {e.name: await registry_view(runtime, e) for e in await runtime.device_registry.list_all()}
        engine = rows["listed_sim_a"].engine

        assert engine is not None and engine.effective_mode is WorkflowRunMode.SIM
    finally:
        hold.set()
        await runtime.shutdown()
