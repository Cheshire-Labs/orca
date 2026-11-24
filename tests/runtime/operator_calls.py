"""Send a command by name through its Operation, with a facade under test and a runner that records what it sent."""

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from pydantic import JsonValue

from orca.gateway.device_command_runner import NamedCommandResult
from orca.gateway.registry.snapshot import DeviceSnapshot
from orca.operations.operator_device import DeviceBatchExecuteOperation
from orca.operations.operator_device_models import BatchExecuteRequest, BatchExecuteResponse
from orca.runtime.facades.devices import DeviceFacade
from orca.modes.run_modes import WorkflowRunMode


def recording_runner(*, fails_with: Exception | list[Exception | NamedCommandResult] | None = None) -> MagicMock:
    """A runner that records each command; `fails_with` raises, or is a sequence of outcomes one per send."""
    runner = MagicMock()
    runner.run_named = AsyncMock(
        return_value=NamedCommandResult(duration_ms=0.0), side_effect=fails_with,
    )
    runner.device = AsyncMock(side_effect=lambda device_id: DeviceSnapshot(
        type="Driver", name=device_id, interfaces=[], capabilities=[], provides_state=False, methods={},
        site="test", lab="test", workcell=None, status="ready", last_seen=datetime.now(timezone.utc),
    ))

    @asynccontextmanager
    async def hold(device_id: str) -> AsyncIterator[MagicMock]:
        runner.held.append(device_id)
        yield runner

    runner.held = []
    runner.hold = hold
    return runner


def _runtime(facade: DeviceFacade) -> MagicMock:
    runtime = MagicMock()
    runtime.devices = facade
    runtime.operator_base_mode = MagicMock(return_value=WorkflowRunMode.LIVE)
    return runtime


async def send(
    facade: DeviceFacade, runner: MagicMock, device: str, command: str,
    params: dict[str, JsonValue] | None = None, *, vendor_confirm: bool = False, reason: str | None = None,
) -> BatchExecuteResponse:
    """One command sent by name: a batch of one."""
    command_json: dict[str, JsonValue] = {"command": command, "params": params or {}, "confirm": vendor_confirm}
    return await DeviceBatchExecuteOperation(runtime=_runtime(facade), devices=runner).run(
        BatchExecuteRequest(device_id=device, commands=[command_json], stop_on_error=True, reason=reason),
    )
