"""Unit tests for BatchExecutor.

Dispatch resolution and the external-control flag are stubbed here: what these
pin is the executor's own retry, stop-on-error and aggregation behaviour. That a
batched command records in the ledger and holds the flag is pinned against a
real runtime in ``tests/test_adhoc_commands_are_recorded.py``.
"""

from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.runtime.run_modes import WorkflowRunMode

from orca.gateway.controller.exceptions import (
    CommandExecutionError,
    CommandTimeoutError,
    DeviceLockedError,
    DeviceOfflineError,
)
from orca.gateway.batch import BatchExecutor
from orca.gateway.batch import CommandResult, DeviceCommand
from tests.gateway._control_scope import stub_control_scope


@pytest.fixture
def mock_controller() -> MagicMock:
    """Create a mock DeviceController."""
    controller = MagicMock()
    controller.execute_command = AsyncMock()
    return controller


@pytest.fixture
def executor(
    mock_controller: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> BatchExecutor:
    """A BatchExecutor whose control scope resolves without a real topology."""
    stub_control_scope(monkeypatch, mock_controller)
    return BatchExecutor(mock_controller, lambda _name: WorkflowRunMode.LIVE)


class TestBatchExecutorSuccess:
    """Test successful batch execution."""

    @pytest.mark.asyncio
    async def test_execute_empty_batch(
        self, executor: BatchExecutor
    ) -> None:
        result = await executor.execute_batch([])
        assert result.success is True
        assert result.results == []
        assert result.failed_at_index is None
        assert result.total_duration_ms >= 0

    @pytest.mark.asyncio
    async def test_execute_single_command(
        self,
        executor: BatchExecutor,
        mock_controller: MagicMock,
    ) -> None:
        mock_controller.execute_command.return_value = {"status": "ok"}

        commands = [
            DeviceCommand(device_id="shaker-1", command="shake", params={"speed": 500, "duration": 30})
        ]

        result = await executor.execute_batch(commands)

        assert result.success is True
        assert len(result.results) == 1
        assert result.results[0].success is True
        assert result.results[0].result == {"status": "ok"}
        assert result.failed_at_index is None

        mock_controller.execute_command.assert_called_once_with(
            device_id="shaker-1",
            command="shake",
            params={"speed": 500, "duration": 30},
            timeout_seconds=None,
            effective_mode=WorkflowRunMode.LIVE,
            effective_interfaces=None,
            confirm=False,
        )

    @pytest.mark.asyncio
    async def test_execute_multiple_commands(
        self,
        executor: BatchExecutor,
        mock_controller: MagicMock,
    ) -> None:
        mock_controller.execute_command.return_value = {"status": "ok"}

        commands = [
            DeviceCommand(device_id="shaker-1", command="open"),
            DeviceCommand(device_id="shaker-1", command="lock_plate"),
            DeviceCommand(device_id="shaker-1", command="shake", params={"speed": 500, "duration": 30}),
            DeviceCommand(device_id="shaker-1", command="unlock_plate"),
            DeviceCommand(device_id="shaker-1", command="close"),
        ]

        result = await executor.execute_batch(commands)

        assert result.success is True
        assert len(result.results) == 5
        assert all(r.success for r in result.results)
        assert result.failed_at_index is None
        assert mock_controller.execute_command.call_count == 5

    @pytest.mark.asyncio
    async def test_execute_with_timeout(
        self,
        executor: BatchExecutor,
        mock_controller: MagicMock,
    ) -> None:
        mock_controller.execute_command.return_value = {"status": "ok"}

        commands = [
            DeviceCommand(device_id="centrifuge-1", command="centrifuge", params={"g": 1000}, timeout=60.0)
        ]

        result = await executor.execute_batch(commands)

        assert result.success is True
        mock_controller.execute_command.assert_called_with(
            device_id="centrifuge-1",
            command="centrifuge",
            params={"g": 1000},
            timeout_seconds=60.0,
            effective_mode=WorkflowRunMode.LIVE,
            effective_interfaces=None,
            confirm=False,
        )


class TestBatchExecutorErrors:
    """Test error handling in batch execution."""

    @pytest.mark.asyncio
    async def test_stop_on_error_true(
        self,
        executor: BatchExecutor,
        mock_controller: MagicMock,
    ) -> None:
        # Second command fails
        mock_controller.execute_command.side_effect = [
            {"status": "ok"},
            DeviceOfflineError("Device disconnected"),
            {"status": "ok"},  # Should not be reached
        ]

        commands = [
            DeviceCommand(device_id="shaker-1", command="open"),
            DeviceCommand(device_id="shaker-1", command="shake", params={"speed": 500}),
            DeviceCommand(device_id="shaker-1", command="close"),
        ]

        result = await executor.execute_batch(commands, stop_on_error=True)

        assert result.success is False
        assert len(result.results) == 2  # Stopped after second command
        assert result.results[0].success is True
        assert result.results[1].success is False
        assert result.results[1].error_type == "DeviceOfflineError"
        assert result.failed_at_index == 1
        assert mock_controller.execute_command.call_count == 2

    @pytest.mark.asyncio
    async def test_stop_on_error_false(
        self,
        executor: BatchExecutor,
        mock_controller: MagicMock,
    ) -> None:
        # Second command fails, but continue
        mock_controller.execute_command.side_effect = [
            {"status": "ok"},
            DeviceOfflineError("Device disconnected"),
            {"status": "ok"},  # Should still execute
        ]

        commands = [
            DeviceCommand(device_id="shaker-1", command="open"),
            DeviceCommand(device_id="shaker-1", command="shake", params={"speed": 500}),
            DeviceCommand(device_id="shaker-1", command="close"),
        ]

        result = await executor.execute_batch(commands, stop_on_error=False)

        assert result.success is False
        assert len(result.results) == 3  # All commands attempted
        assert result.results[0].success is True
        assert result.results[1].success is False
        assert result.results[2].success is True
        assert result.failed_at_index == 1  # First failure
        assert mock_controller.execute_command.call_count == 3

    @pytest.mark.asyncio
    async def test_device_offline_error(
        self,
        executor: BatchExecutor,
        mock_controller: MagicMock,
    ) -> None:
        mock_controller.execute_command.side_effect = DeviceOfflineError("Device not connected")

        commands = [DeviceCommand(device_id="shaker-1", command="shake")]
        result = await executor.execute_batch(commands)

        assert result.success is False
        assert result.results[0].success is False
        assert result.results[0].error_type == "DeviceOfflineError"
        assert result.results[0].error is not None
        assert "not connected" in result.results[0].error

    @pytest.mark.asyncio
    async def test_command_timeout_error(
        self,
        executor: BatchExecutor,
        mock_controller: MagicMock,
    ) -> None:
        mock_controller.execute_command.side_effect = CommandTimeoutError("Operation timed out")

        commands = [DeviceCommand(device_id="centrifuge-1", command="centrifuge")]
        result = await executor.execute_batch(commands)

        assert result.success is False
        assert result.results[0].success is False
        assert result.results[0].error_type == "CommandTimeoutError"

    @pytest.mark.asyncio
    async def test_command_execution_error(
        self,
        executor: BatchExecutor,
        mock_controller: MagicMock,
    ) -> None:
        error = CommandExecutionError("Motor stalled", error_type="MotorError")
        mock_controller.execute_command.side_effect = error

        commands = [DeviceCommand(device_id="shaker-1", command="shake")]
        result = await executor.execute_batch(commands)

        assert result.success is False
        assert result.results[0].success is False
        assert result.results[0].error_type == "MotorError"
        assert result.results[0].error == "Motor stalled"


class TestBatchExecutorRetry:
    """Test retry logic for DeviceLockedError."""

    @pytest.mark.asyncio
    async def test_retry_on_device_locked_success(
        self,
        executor: BatchExecutor,
        mock_controller: MagicMock,
    ) -> None:
        # Fail twice with DeviceLockedError, then succeed
        mock_controller.execute_command.side_effect = [
            DeviceLockedError("Device busy"),
            DeviceLockedError("Device still busy"),
            {"status": "ok"},
        ]

        # Reduce delays for faster test
        executor.INITIAL_RETRY_DELAY = 0.01
        executor.MAX_RETRY_DELAY = 0.01

        commands = [DeviceCommand(device_id="shaker-1", command="shake")]
        result = await executor.execute_batch(commands)

        assert result.success is True
        assert result.results[0].success is True
        assert mock_controller.execute_command.call_count == 3

    @pytest.mark.asyncio
    async def test_retry_exhausted(
        self,
        executor: BatchExecutor,
        mock_controller: MagicMock,
    ) -> None:
        # Always fail with DeviceLockedError
        mock_controller.execute_command.side_effect = DeviceLockedError("Device always busy")

        # Reduce delays for faster test
        executor.INITIAL_RETRY_DELAY = 0.01
        executor.MAX_RETRY_DELAY = 0.01

        commands = [DeviceCommand(device_id="shaker-1", command="shake")]
        result = await executor.execute_batch(commands)

        assert result.success is False
        assert result.results[0].success is False
        assert result.results[0].error_type == "DeviceLockedError"
        # Should have tried MAX_LOCK_RETRIES + 1 times (initial + retries)
        assert mock_controller.execute_command.call_count == executor.MAX_LOCK_RETRIES + 1

    @pytest.mark.asyncio
    async def test_no_retry_for_other_errors(
        self,
        executor: BatchExecutor,
        mock_controller: MagicMock,
    ) -> None:
        # DeviceOfflineError should not be retried
        mock_controller.execute_command.side_effect = DeviceOfflineError("Device offline")

        commands = [DeviceCommand(device_id="shaker-1", command="shake")]
        result = await executor.execute_batch(commands)

        assert result.success is False
        assert mock_controller.execute_command.call_count == 1  # No retry


class TestBatchExecutorProgressCallback:
    """Test progress callback functionality."""

    @pytest.mark.asyncio
    async def test_callback_called_for_each_command(
        self,
        executor: BatchExecutor,
        mock_controller: MagicMock,
    ) -> None:
        mock_controller.execute_command.return_value = {"status": "ok"}

        callback_calls: list[tuple[int, DeviceCommand, Optional[CommandResult]]] = []

        def callback(
            index: int,
            command: DeviceCommand,
            result: Optional[CommandResult],
        ) -> None:
            callback_calls.append((index, command, result))

        commands = [
            DeviceCommand(device_id="shaker-1", command="open"),
            DeviceCommand(device_id="shaker-1", command="shake"),
            DeviceCommand(device_id="shaker-1", command="close"),
        ]

        await executor.execute_batch(commands, progress_callback=callback)

        assert len(callback_calls) == 3
        assert callback_calls[0][0] == 0
        assert callback_calls[1][0] == 1
        assert callback_calls[2][0] == 2

        # All callbacks should have successful results
        assert all(call[2] is not None and call[2].success for call in callback_calls)

    @pytest.mark.asyncio
    async def test_callback_called_even_on_failure(
        self,
        executor: BatchExecutor,
        mock_controller: MagicMock,
    ) -> None:
        mock_controller.execute_command.side_effect = [
            {"status": "ok"},
            DeviceOfflineError("Disconnected"),
        ]

        callback_calls: list[tuple[int, DeviceCommand, Optional[CommandResult]]] = []

        def callback(
            index: int,
            command: DeviceCommand,
            result: Optional[CommandResult],
        ) -> None:
            callback_calls.append((index, command, result))

        commands = [
            DeviceCommand(device_id="shaker-1", command="open"),
            DeviceCommand(device_id="shaker-1", command="shake"),
        ]

        await executor.execute_batch(
            commands, stop_on_error=True, progress_callback=callback
        )

        assert len(callback_calls) == 2
        assert callback_calls[0][2] is not None and callback_calls[0][2].success is True
        assert callback_calls[1][2] is not None and callback_calls[1][2].success is False


class TestDeviceCommand:
    """Test DeviceCommand model.

    The all-fields-set case is covered through real executor use by
    TestBatchExecutorSuccess.test_execute_single_command (device_id /
    command / params forwarded verbatim) and test_execute_with_timeout
    (timeout forwarded as timeout_seconds), so no ctor-echo test for it.
    """

    def test_create_command_minimal(self) -> None:
        cmd = DeviceCommand(device_id="centrifuge-1", command="stop")

        assert cmd.device_id == "centrifuge-1"
        assert cmd.command == "stop"
        assert cmd.params is None
        assert cmd.timeout is None

    def test_create_command_with_empty_params(self) -> None:
        cmd = DeviceCommand(device_id="sealer-1", command="open", params={})

        assert cmd.device_id == "sealer-1"
        assert cmd.command == "open"
        assert cmd.params == {}


class TestCommandResult:
    """CommandResult fields are populated by the executor, not the ctor."""

    @pytest.mark.asyncio
    async def test_successful_result_carries_controller_return_and_command(
        self,
        executor: BatchExecutor,
        mock_controller: MagicMock,
    ) -> None:
        mock_controller.execute_command.return_value = {"status": "ok"}
        cmd = DeviceCommand(device_id="shaker-1", command="shake")

        batch = await executor.execute_batch([cmd])
        result = batch.results[0]

        assert result.command is cmd
        assert result.success is True
        assert result.result == {"status": "ok"}
        assert result.error is None
        assert result.error_type is None
        assert result.duration_ms >= 0.0

    @pytest.mark.asyncio
    async def test_failed_result_carries_error_message_and_type(
        self,
        executor: BatchExecutor,
        mock_controller: MagicMock,
    ) -> None:
        mock_controller.execute_command.side_effect = DeviceOfflineError(
            "Device not found"
        )
        cmd = DeviceCommand(device_id="shaker-1", command="shake")

        batch = await executor.execute_batch([cmd])
        result = batch.results[0]

        assert result.command is cmd
        assert result.success is False
        assert result.error == "Device not found"
        assert result.error_type == "DeviceOfflineError"
        assert result.result is None


class TestBatchResult:
    """BatchResult fields are populated by the executor, not the ctor."""

    @pytest.mark.asyncio
    async def test_executor_reports_all_success_aggregate(
        self,
        executor: BatchExecutor,
        mock_controller: MagicMock,
    ) -> None:
        mock_controller.execute_command.return_value = {"status": "ok"}
        commands = [
            DeviceCommand(device_id="shaker-1", command="open"),
            DeviceCommand(device_id="shaker-1", command="close"),
        ]

        batch = await executor.execute_batch(commands)

        assert batch.success is True
        assert batch.failed_at_index is None
        assert batch.commands_executed == 2
        assert batch.commands_executed == len(batch.results)
        assert [r.success for r in batch.results] == [True, True]
        assert batch.total_duration_ms >= 0.0

    @pytest.mark.asyncio
    async def test_failed_batch_aggregates_first_failure(
        self,
        executor: BatchExecutor,
        mock_controller: MagicMock,
    ) -> None:
        """The executor flips overall success off and records the index of the
        first failing command while continuing under stop_on_error=False."""
        mock_controller.execute_command.side_effect = [
            {"status": "ok"},
            DeviceOfflineError("Disconnected"),
            {"status": "ok"},
        ]
        commands = [
            DeviceCommand(device_id="shaker-1", command="open"),
            DeviceCommand(device_id="shaker-1", command="shake"),
            DeviceCommand(device_id="shaker-1", command="close"),
        ]

        batch = await executor.execute_batch(commands, stop_on_error=False)

        assert batch.success is False
        assert batch.failed_at_index == 1
        assert batch.commands_executed == 3
        assert [r.success for r in batch.results] == [True, False, True]
        assert batch.results[1].error_type == "DeviceOfflineError"
        assert batch.total_duration_ms >= 0.0
