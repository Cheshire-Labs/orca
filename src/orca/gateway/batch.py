"""Generic batch executor over the device controller.

Runs a list of device commands sequentially with shared error handling
(retry on ``DeviceLockedError``, stop-on-error policy, per-command progress
callback), plus the three dataclasses it consumes.

Dispatch goes through ``orca.gateway.adhoc``, the same path a single operator
command takes, so a batched command records in the ledger and holds the
external-control flag. The flag is taken once for the run rather than per
command: a batch is one operator intent, and releasing between commands lets
the engine schedule the device in the middle of it.
"""

import asyncio
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Callable, Optional

from pydantic import JsonValue

from orca.gateway.adhoc import OperatorControl, operator_control
from orca.gateway.controller.controller import DeviceController
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.run_modes import WorkflowRunMode
from orca.gateway.controller.exceptions import (
    ConfirmationRequiredError,
    DeviceLockedError,
    DeviceOfflineError,
    CommandTimeoutError,
    CommandExecutionError,
)


@dataclass
class DeviceCommand:
    """A single command to be executed on a device.

    ``confirm`` is the caller's acknowledgement for a vendor command the
    catalog flags ``requires_confirm``; without it the controller refuses
    the command before any wire send.
    """

    device_id: str
    command: str
    params: Optional[dict[str, JsonValue]] = None
    timeout: Optional[float] = None
    confirm: bool = False


@dataclass
class CommandResult:
    """Result of executing a single device command."""

    command: DeviceCommand
    success: bool
    result: Optional[JsonValue] = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    duration_ms: float = 0.0


@dataclass
class BatchResult:
    """Result of executing a batch of commands."""

    success: bool
    results: list[CommandResult] = field(default_factory=list)
    failed_at_index: Optional[int] = None
    total_duration_ms: float = 0.0

    @property
    def commands_executed(self) -> int:
        """Number of commands that were executed (success or failure)."""
        return len(self.results)


# Type alias for optional progress callback
ProgressCallback = Callable[[int, DeviceCommand, Optional[CommandResult]], None]


class BatchExecutor:
    """Runs a sequential batch of device commands with error handling.

    Sequential execution with optional stop-on-error, DeviceLockedError
    retry (exponential backoff), per-command progress callbacks, and
    per-command result collection. The hosting layer's batch
    device-command REST/MCP drives it.
    """

    # Retry configuration for DeviceLockedError
    MAX_LOCK_RETRIES = 3
    INITIAL_RETRY_DELAY = 2.0  # seconds
    MAX_RETRY_DELAY = 8.0  # seconds

    def __init__(
        self,
        device_controller: DeviceController,
        mode_resolver: Callable[[str], WorkflowRunMode],
        runtime: ISystemRuntime | None = None,
    ):
        """
        Initialize the batch executor.

        Args:
            device_controller: Controller for executing device commands.
            mode_resolver: Per-device run mode. Only reached when there is no
                runtime to resolve one from; build it with
                `orca.gateway.mode_resolution.system_mode_resolver`.
            runtime: The runtime whose ledger each command is recorded in, and
                whose topology carries the external-control flag. Without one a
                command still runs, and leaves no trail.
        """
        self._controller = device_controller
        self._mode_resolver = mode_resolver
        self._runtime = runtime

    async def execute_batch(
        self,
        commands: list[DeviceCommand],
        stop_on_error: bool = True,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> BatchResult:
        """Execute a batch of commands sequentially."""
        results: list[CommandResult] = []
        batch_start = time.perf_counter()
        failed_at_index: Optional[int] = None
        overall_success = True

        async with AsyncExitStack() as scopes:
            controls: dict[str, object] = {}
            for index, command in enumerate(commands):
                # One scope per device, opened on first use and held to the end
                # of the batch. A batch is normally one device; the map is what
                # keeps a mixed one correct rather than assuming otherwise.
                if command.device_id not in controls:
                    controls[command.device_id] = await scopes.enter_async_context(
                        operator_control(
                            self._runtime, command.device_id, self._controller,
                        )
                    )
                cmd_result = await self._execute_with_retry(
                    command, controls[command.device_id],
                )
                results.append(cmd_result)

                # Call progress callback if provided
                if progress_callback is not None:
                    progress_callback(index, command, cmd_result)

                if not cmd_result.success:
                    overall_success = False
                    failed_at_index = index

                    if stop_on_error:
                        break

        batch_end = time.perf_counter()
        total_duration_ms = (batch_end - batch_start) * 1000

        return BatchResult(
            success=overall_success,
            results=results,
            failed_at_index=failed_at_index,
            total_duration_ms=total_duration_ms,
        )

    async def _execute_with_retry(
        self,
        command: DeviceCommand,
        control: OperatorControl,
    ) -> CommandResult:
        """
        Execute a single command with retry logic for DeviceLockedError.

        Retry strategy:
        - DeviceLockedError: Retry with exponential backoff (2^attempt seconds, up to MAX_LOCK_RETRIES retries after the initial attempt)
        - DeviceOfflineError: No retry (device disconnected)
        - CommandTimeoutError: No retry (operation timed out)
        - CommandExecutionError: No retry (device-level error, safe to surface)

        Args:
            command: The command to execute

        Returns:
            CommandResult with success/failure status and any error details
        """
        last_error: Optional[Exception] = None
        attempt = 0

        while attempt <= self.MAX_LOCK_RETRIES:
            cmd_start = time.perf_counter()

            try:
                result = (await control.run(
                    command.command,
                    command.params,
                    timeout_seconds=command.timeout,
                    confirm=command.confirm,
                )).raw

                cmd_end = time.perf_counter()
                duration_ms = (cmd_end - cmd_start) * 1000

                return CommandResult(
                    command=command,
                    success=True,
                    result=result,
                    duration_ms=duration_ms,
                )

            except DeviceLockedError as e:
                last_error = e
                attempt += 1

                if attempt > self.MAX_LOCK_RETRIES:
                    # Max retries exceeded
                    break

                # Exponential backoff: 2, 4, 8 seconds (capped at MAX_RETRY_DELAY)
                delay = min(
                    self.INITIAL_RETRY_DELAY * (2 ** (attempt - 1)),
                    self.MAX_RETRY_DELAY,
                )
                await asyncio.sleep(delay)

            except ConfirmationRequiredError as e:
                # No retry: a missing confirm never heals on its own.
                cmd_end = time.perf_counter()
                return CommandResult(
                    command=command,
                    success=False,
                    error=str(e),
                    error_type="ConfirmationRequiredError",
                    duration_ms=(cmd_end - cmd_start) * 1000,
                )

            except DeviceOfflineError as e:
                cmd_end = time.perf_counter()
                return CommandResult(
                    command=command,
                    success=False,
                    error=str(e),
                    error_type="DeviceOfflineError",
                    duration_ms=(cmd_end - cmd_start) * 1000,
                )

            except CommandTimeoutError as e:
                cmd_end = time.perf_counter()
                return CommandResult(
                    command=command,
                    success=False,
                    error=str(e),
                    error_type="CommandTimeoutError",
                    duration_ms=(cmd_end - cmd_start) * 1000,
                )

            except CommandExecutionError as e:
                cmd_end = time.perf_counter()
                return CommandResult(
                    command=command,
                    success=False,
                    error=str(e),
                    error_type=e.error_type,
                    duration_ms=(cmd_end - cmd_start) * 1000,
                )

        # If we get here, we exhausted retries for DeviceLockedError
        return CommandResult(
            command=command,
            success=False,
            error=str(last_error) if last_error else "Device locked after max retries",
            error_type="DeviceLockedError",
            duration_ms=0.0,  # Duration not meaningful after retries
        )
