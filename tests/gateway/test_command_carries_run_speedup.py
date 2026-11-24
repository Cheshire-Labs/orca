"""A command sent to a device bridge carries the run's sim speed-up, so a device sim paces at it."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from pydantic import JsonValue

from cheshire_drivers.gateway_protocol import CommandMessage, MessageEnvelope
from orca.gateway.controller import DeviceController
from tests.gateway_parts import new_controller
from orca.gateway.registry.snapshot import DeviceSnapshot
from orca.modes.run_modes import WorkflowRunMode
from orca.modes.sim_pacing import SimPacer, current_sim_pacer
from tests.test_helpers import wait_until

@pytest.fixture
def mock_registry() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_conn_manager() -> MagicMock:
    return MagicMock()



def _shaker() -> DeviceSnapshot:
    return DeviceSnapshot(
        type="shaker", name="shaker_1", interfaces=["IShaker"], capabilities=[],
        provides_state=False, methods={}, site="test", lab="test", workcell=None,
        status="ready", last_seen=datetime.utcnow(),
    )


def _wire_up(mock_registry: Mock, mock_conn_manager: Mock) -> None:
    mock_registry.get_device = AsyncMock(return_value=_shaker())
    mock_registry.is_device_online = AsyncMock(return_value=True)
    mock_registry.get_client_for_device = AsyncMock(return_value="client_1")
    mock_conn_manager.send_to_client = AsyncMock(return_value=True)


def _sent_speedup(mock_conn_manager: Mock) -> float | None:
    envelope = MessageEnvelope.model_validate_json(mock_conn_manager.send_to_client.call_args[0][1])
    return CommandMessage.model_validate(envelope.payload).sim_speedup


def _start_shake(controller: DeviceController, pacer: SimPacer | None) -> asyncio.Task[JsonValue]:
    token = current_sim_pacer.set(pacer)
    try:
        return asyncio.create_task(controller.execute_command(
            "shaker_1", "shake", {"speed": 500.0, "duration": 1.0},
            timeout_seconds=30, effective_mode=WorkflowRunMode.SIM, execution_id="exec_1",
        ))
    finally:
        current_sim_pacer.reset(token)


async def _stop(task: asyncio.Task[JsonValue]) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_a_command_under_a_run_pacer_carries_its_speedup(mock_registry: Mock, mock_conn_manager: Mock) -> None:
    _wire_up(mock_registry, mock_conn_manager)
    controller = new_controller(mock_registry, mock_conn_manager)
    task = _start_shake(controller, SimPacer(0.5))
    await wait_until(lambda: mock_conn_manager.send_to_client.called)
    assert _sent_speedup(mock_conn_manager) == 0.5
    await _stop(task)


@pytest.mark.asyncio
async def test_a_command_with_no_run_pacer_carries_no_speedup(mock_registry: Mock, mock_conn_manager: Mock) -> None:
    _wire_up(mock_registry, mock_conn_manager)
    controller = new_controller(mock_registry, mock_conn_manager)
    task = _start_shake(controller, None)
    await wait_until(lambda: mock_conn_manager.send_to_client.called)
    assert _sent_speedup(mock_conn_manager) is None
    await _stop(task)
