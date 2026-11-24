"""An operator command goes in the active run's mode because the runtime hands every device and the wire stamp a
base, not because each entry point remembers to open a scope."""

from collections.abc import Mapping
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import JsonValue

from orca.gateway import operator_recorder
from orca.gateway.mode_resolution import system_mode_resolver
from orca.gateway.operator_commands import OperatorControl
from orca.gateway.operator_recorder import RuntimeOperatorCommandRecorder
from orca.resource_models.resources import IModeAware
from orca.modes.device_modes import DeviceMode
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.system.system_interface import ISystem

from tests.device_modes import set_to
from tests.gateway.mode_doubles import unseeded_await
from tests.gateway.test_an_operator_sim_command_waits_at_the_operator_speedup import _shaker
from tests.memory_activity import memory_runtime
from tests.test_system_runtime import _build_simple_system


def _mode_aware(system: ISystem, name: str) -> IModeAware:
    resource = system.get_resource(name)
    assert isinstance(resource, IModeAware)
    return resource


async def _runtime_with(shaker: DeviceMode, arm: DeviceMode) -> SystemRuntime:
    system, _ = await _build_simple_system()
    set_to(_mode_aware(system, "shaker1"), "shaker1", shaker)
    set_to(_mode_aware(system, "robot1"), "robot1", arm)
    return await memory_runtime(system)


async def _runtime_in_a_sim_run(monkeypatch: pytest.MonkeyPatch) -> SystemRuntime:
    """Both devices are set to live; a SIM run is active."""
    runtime = await _runtime_with(DeviceMode.LIVE, DeviceMode.LIVE)
    monkeypatch.setattr(runtime, "operator_base_mode", lambda: WorkflowRunMode.SIM)
    return runtime


@pytest.mark.asyncio
async def test_a_device_read_outside_any_scope_goes_in_the_active_runs_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = await _runtime_in_a_sim_run(monkeypatch)

    assert await unseeded_await(_mode_aware(runtime.system, "robot1").effective_mode()) is WorkflowRunMode.SIM


@pytest.mark.asyncio
async def test_the_wire_stamp_reads_the_base_on_every_command(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = await _runtime_in_a_sim_run(monkeypatch)
    base = [WorkflowRunMode.SIM]
    resolve = system_mode_resolver(lambda: runtime, base_mode=lambda: base[0])

    in_the_run = await unseeded_await(resolve("robot1"))
    base[0] = WorkflowRunMode.LIVE
    after_it = await unseeded_await(resolve("robot1"))

    assert (in_the_run, after_it) == (WorkflowRunMode.SIM, WorkflowRunMode.LIVE)


@pytest.mark.asyncio
async def test_an_operator_commands_record_is_saved_in_the_operators_world_not_the_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shaker is set to sim and the arm to live, with no run: the arm's world in the save is live."""
    runtime = await _runtime_with(DeviceMode.SIM, DeviceMode.LIVE)
    seen: list[WorkflowRunMode] = []

    async def record(_system: ISystem, *_args: str | Mapping[str, JsonValue], **_kwargs: JsonValue | frozenset[str]) -> None:
        seen.append(await _mode_aware(runtime.system, "robot1").effective_mode())

    monkeypatch.setattr(operator_recorder, "record_operator_command", record)
    controller = MagicMock()
    controller.execute_command = AsyncMock(return_value=None)
    control = OperatorControl(
        runtime, "shaker1", _shaker().model_copy(update={"name": "shaker1"}), runtime.system.get_resource("shaker1"),
        WorkflowRunMode.SIM,
        RuntimeOperatorCommandRecorder(runtime), controller,
    )

    await unseeded_await(control.run("shake", {"speed": 500.0, "duration": 1.0}))

    assert seen == [WorkflowRunMode.LIVE]
