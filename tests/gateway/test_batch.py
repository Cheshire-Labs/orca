"""Unit tests for the batch Operation's loop, `run_batch`, over the operator command runner.

Dispatch resolution and the external-control flag are stubbed here: what these pin is the batch's own retry,
stop-on-error and aggregation behaviour. That a batched command records in
the ledger and holds the flag is pinned against a real runtime in ``tests/test_adhoc_commands_are_recorded.py``.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import JsonValue

from orca.gateway.controller.exceptions import (
    CommandExecutionError,
    CommandTimeoutError,
    DeviceLockedError,
    DeviceOfflineError,
)
from orca.events.execution_context import DeviceCommandContext
from orca.gateway.controller.command_events import current_command
from orca.gateway.operator_commands import OperatorDeviceCommands
from orca.gateway.registry.snapshot import DeviceSnapshot
from orca.runtime.server_events import ServerEvents
from orca.events.runtime_event import RuntimeEvent
from orca.operations.operator_device import BatchCommand, run_batch
from orca.operations.operator_device_models import BatchCommandResultDTO
from orca.modes.run_modes import WorkflowRunMode
from tests.gateway._control_scope import stub_control_scope
from tests.gateway_parts import gateway_with
from tests.named_commands import named_commands

DEVICE = "shaker-1"


def _knows_the_shaker() -> MagicMock:
    """A connection tracker that has the shaker registered, so the batch can read it before each command."""
    tracker = MagicMock()
    tracker.get_device = AsyncMock(return_value=DeviceSnapshot(
        type="shaker", name=DEVICE, interfaces=["IShaker"], capabilities=[], provides_state=False, methods={},
        site="test", lab="test", workcell=None, status="ready", last_seen=datetime.now(timezone.utc),
    ))
    return tracker


@dataclass(frozen=True)
class _Batch:
    results: list[BatchCommandResultDTO]
    failed_at_index: int | None
    total_duration_ms: float

    @property
    def success(self) -> bool:
        return self.failed_at_index is None


class _Executor:
    def __init__(self, controller: MagicMock) -> None:
        self._runner = OperatorDeviceCommands(gateway_with(tracker=_knows_the_shaker(), controller=controller), lambda: None)

    async def execute_batch(self, commands: list[BatchCommand], stop_on_error: bool = True) -> _Batch:
        results, failed_at, total_ms = await run_batch(
            named_commands(self._runner, DEVICE), commands, stop_on_error=stop_on_error, reason=None,
        )
        return _Batch(results, failed_at, total_ms)


@pytest.fixture
def mock_controller() -> MagicMock:
    controller = MagicMock()
    controller.execute_command = AsyncMock()
    return controller


@pytest.fixture
def executor(mock_controller: MagicMock, monkeypatch: pytest.MonkeyPatch) -> _Executor:
    """A batch whose control scope resolves without a real topology."""
    stub_control_scope(monkeypatch)
    return _Executor(mock_controller)


class TestBatchSuccess:
    @pytest.mark.asyncio
    async def test_execute_empty_batch(self, executor: _Executor) -> None:
        result = await executor.execute_batch([])
        assert result.success is True
        assert result.results == []
        assert result.failed_at_index is None
        assert result.total_duration_ms >= 0

    @pytest.mark.asyncio
    async def test_execute_single_command(self, executor: _Executor, mock_controller: MagicMock) -> None:
        mock_controller.execute_command.return_value = {"status": "ok"}

        result = await executor.execute_batch([BatchCommand(command="shake", params={"speed": 500, "duration": 30})])

        assert result.success is True
        assert len(result.results) == 1
        assert result.results[0].success is True
        assert result.results[0].result == {"status": "ok"}
        mock_controller.execute_command.assert_called_once_with(
            device_id=DEVICE,
            command="shake",
            params={"speed": 500, "duration": 30},
            timeout_seconds=None,
            effective_mode=WorkflowRunMode.LIVE,
            effective_interfaces=None,
            confirm=False,
        )

    @pytest.mark.asyncio
    async def test_execute_multiple_commands(self, executor: _Executor, mock_controller: MagicMock) -> None:
        mock_controller.execute_command.return_value = {"status": "ok"}
        names = ["open", "lock_plate", "shake", "unlock_plate", "close"]

        result = await executor.execute_batch([BatchCommand(command=n) for n in names])

        assert result.success is True
        assert [r.command for r in result.results] == names
        assert mock_controller.execute_command.call_count == 5

    @pytest.mark.asyncio
    async def test_execute_with_timeout(self, executor: _Executor, mock_controller: MagicMock) -> None:
        mock_controller.execute_command.return_value = {"status": "ok"}

        result = await executor.execute_batch([BatchCommand(command="centrifuge", params={"g": 1000}, timeout=60.0)])

        assert result.success is True
        assert mock_controller.execute_command.call_args.kwargs["timeout_seconds"] == 60.0


class TestBatchErrors:
    @pytest.mark.asyncio
    async def test_stop_on_error_true(self, executor: _Executor, mock_controller: MagicMock) -> None:
        mock_controller.execute_command.side_effect = [
            {"status": "ok"}, DeviceOfflineError("Device disconnected"), {"status": "ok"},
        ]
        commands = [BatchCommand(command="open"), BatchCommand(command="shake"), BatchCommand(command="close")]

        result = await executor.execute_batch(commands, stop_on_error=True)

        assert result.success is False
        assert [r.success for r in result.results] == [True, False]
        assert result.results[1].error_type == "DeviceOfflineError"
        assert result.failed_at_index == 1
        assert mock_controller.execute_command.call_count == 2

    @pytest.mark.asyncio
    async def test_stop_on_error_false(self, executor: _Executor, mock_controller: MagicMock) -> None:
        mock_controller.execute_command.side_effect = [
            {"status": "ok"}, DeviceOfflineError("Device disconnected"), {"status": "ok"},
        ]
        commands = [BatchCommand(command="open"), BatchCommand(command="shake"), BatchCommand(command="close")]

        result = await executor.execute_batch(commands, stop_on_error=False)

        assert [r.success for r in result.results] == [True, False, True]
        assert result.failed_at_index == 1
        assert mock_controller.execute_command.call_count == 3

    @pytest.mark.asyncio
    async def test_device_offline_error(self, executor: _Executor, mock_controller: MagicMock) -> None:
        mock_controller.execute_command.side_effect = DeviceOfflineError("Device not connected")

        result = await executor.execute_batch([BatchCommand(command="shake")])

        assert result.results[0].error_type == "DeviceOfflineError"
        assert "not connected" in (result.results[0].error or "")

    @pytest.mark.asyncio
    async def test_command_timeout_error(self, executor: _Executor, mock_controller: MagicMock) -> None:
        mock_controller.execute_command.side_effect = CommandTimeoutError("Operation timed out")

        result = await executor.execute_batch([BatchCommand(command="centrifuge")])

        assert result.results[0].error_type == "CommandTimeoutError"

    @pytest.mark.asyncio
    async def test_command_execution_error(self, executor: _Executor, mock_controller: MagicMock) -> None:
        mock_controller.execute_command.side_effect = CommandExecutionError("Motor stalled", error_type="MotorError")

        result = await executor.execute_batch([BatchCommand(command="shake")])

        assert result.results[0].error_type == "MotorError"
        assert result.results[0].error == "Motor stalled"
        assert result.results[0].result is None


class TestALockedDevice:
    @pytest.mark.asyncio
    async def test_a_locked_device_fails_the_command_at_once(self, executor: _Executor, mock_controller: MagicMock) -> None:
        """An operator is refused while the engine holds the device, never queued."""
        mock_controller.execute_command.side_effect = DeviceLockedError("Device busy")

        result = await executor.execute_batch([BatchCommand(command="shake")])

        assert result.results[0].error_type == "DeviceLockedError"
        assert mock_controller.execute_command.call_count == 1

    @pytest.mark.asyncio
    async def test_no_retry_for_other_errors(self, executor: _Executor, mock_controller: MagicMock) -> None:
        mock_controller.execute_command.side_effect = DeviceOfflineError("Device offline")

        result = await executor.execute_batch([BatchCommand(command="shake")])

        assert result.success is False
        assert mock_controller.execute_command.call_count == 1


class _Told:
    """Each COMMAND event as (status, command), in order."""

    def __init__(self) -> None:
        self.told: list[tuple[str, str]] = []

    def on_event(self, event: RuntimeEvent) -> None:
        if isinstance(event.context, DeviceCommandContext):
            self.told.append((event.status, event.context.command))


def _reporting(controller: MagicMock, answers: list[JsonValue | Exception]) -> list[tuple[str, str]]:
    """A controller that reports each command to the sender's block as the real one does."""
    seen = _Told()
    events = ServerEvents()
    events.add_sink(seen)
    controller.command_events = events
    pending = list(answers)

    async def execute_command(**kwargs: JsonValue) -> JsonValue:
        block = current_command()
        assert block is not None
        block.reached(events, "SIM", {}, None, None)
        answer = pending.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    controller.execute_command = AsyncMock(side_effect=execute_command)
    return seen.told


class TestBatchEvents:
    @pytest.mark.asyncio
    async def test_each_command_shows_a_start_and_an_end(self, executor: _Executor, mock_controller: MagicMock) -> None:
        told = _reporting(mock_controller, [{"status": "ok"}, {"status": "ok"}])

        await executor.execute_batch([BatchCommand(command="open"), BatchCommand(command="close")])

        assert told == [("STARTED", "open"), ("COMPLETED", "open"), ("STARTED", "close"), ("COMPLETED", "close")]

    @pytest.mark.asyncio
    async def test_a_failed_command_shows_its_failure_and_nothing_after_it(
        self, executor: _Executor, mock_controller: MagicMock,
    ) -> None:
        told = _reporting(mock_controller, [{"status": "ok"}, DeviceOfflineError("Disconnected")])

        await executor.execute_batch(
            [BatchCommand(command="open"), BatchCommand(command="shake"), BatchCommand(command="close")],
            stop_on_error=True,
        )

        assert told == [("STARTED", "open"), ("COMPLETED", "open"), ("STARTED", "shake"), ("FAILED", "shake")]

    @pytest.mark.asyncio
    async def test_a_batch_told_to_keep_going_starts_the_command_after_a_failure(
        self, executor: _Executor, mock_controller: MagicMock,
    ) -> None:
        told = _reporting(mock_controller, [DeviceOfflineError("Disconnected"), {"status": "ok"}])

        await executor.execute_batch(
            [BatchCommand(command="shake"), BatchCommand(command="close")], stop_on_error=False,
        )

        assert told == [("STARTED", "shake"), ("FAILED", "shake"), ("STARTED", "close"), ("COMPLETED", "close")]
