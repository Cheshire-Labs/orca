"""A run's pick, place and site calls run under the run's recoverable timeout, at their declared limit, as a run's
device actions do. A hung arm then raises an incident instead of holding its locks forever."""

from collections.abc import Awaitable, Callable

import pytest
from cheshire_drivers.sims import SimTransporterDriver

from orca.gateway.controller.command_pipeline import DeviceCommandPipeline
from orca.resource_models.resources import EngineCommandLog, engine_time_limit
from orca.runtime.recoverable_timeout import recoverable_timeout_coordinator, within_the_time_limit
from orca.modes.run_modes import current_execution_id
from orca.state.records import OperationRecord

pytestmark = pytest.mark.asyncio


class _Coordinator:
    """Records each limit it is asked to enforce, then runs the call."""

    def __init__(self) -> None:
        self.limits: list[tuple[str, float]] = []

    async def run_with_timeout(
        self, execution_id: str, device_id: str, command: str,
        max_seconds: float, coro_factory: Callable[[], Awaitable[str]],
    ) -> str:
        self.limits.append((command, max_seconds))
        return await coro_factory()

    def extend(self, incident_id: str, additional_seconds: float) -> None: ...

    def abort(self, incident_id: str, operator: str, reason: str) -> None: ...

    def mark_complete(self, incident_id: str, operator: str, reason: str) -> None: ...


async def _save_nowhere(_command: str) -> None:
    return None


def _log() -> EngineCommandLog:
    log = EngineCommandLog("arm_1", lambda command: engine_time_limit(SimTransporterDriver, command))
    pipeline: DeviceCommandPipeline[list[OperationRecord]] = DeviceCommandPipeline(())
    log.save_with(_save_nowhere, pipeline)
    return log


async def _picked() -> str:
    return "picked"


async def _in_a_run(coordinator: _Coordinator, call: Callable[[], Awaitable[str]]) -> str:
    coordinator_token = recoverable_timeout_coordinator.set(coordinator)
    execution_token = current_execution_id.set("exec-1")
    try:
        return await call()
    finally:
        current_execution_id.reset(execution_token)
        recoverable_timeout_coordinator.reset(coordinator_token)


async def test_a_pick_in_a_run_is_bounded_by_its_declared_maximum() -> None:
    coordinator = _Coordinator()

    answer = await _in_a_run(coordinator, lambda: _log().run("pick_at_coords", _picked))

    assert answer == "picked"
    assert coordinator.limits == [("pick_at_coords", 60.0)], "pick_at_coords declares a 60 s maximum"


async def test_an_engine_command_outside_a_run_has_no_run_timer() -> None:
    coordinator = _Coordinator()
    token = recoverable_timeout_coordinator.set(coordinator)
    try:
        await _log().run("pick_at_coords", _picked)
    finally:
        recoverable_timeout_coordinator.reset(token)

    assert coordinator.limits == []


async def test_a_call_already_bounded_starts_no_second_timer() -> None:
    """An engine command inside a run's device action is covered by the action's own timer."""
    coordinator = _Coordinator()
    log = _log()

    await _in_a_run(coordinator, lambda: within_the_time_limit(
        "shaker_1", "shake", 7200.0, lambda: log.run("open", _picked),
    ))

    assert coordinator.limits == [("shake", 7200.0)]


async def test_a_command_that_answers_with_a_value_has_no_run_timer() -> None:
    """Settling its timeout as complete would hand the engine no value to read."""
    coordinator = _Coordinator()

    await _in_a_run(coordinator, lambda: _log().run("get_cartesian_position", _picked))

    assert coordinator.limits == []
