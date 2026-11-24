"""An operator's command on a sim device waits at the speed-up the runtime gives operators: the active run's, else the server default."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from cheshire_drivers.gateway_protocol import CommandMessage, MessageEnvelope
from orca.gateway.operator_commands import OperatorControl
from tests.gateway._control_scope import KeptCommands, empty_runtime
from tests.gateway_parts import new_controller
from orca.gateway.registry.snapshot import DeviceSnapshot
from orca.modes.run_modes import WorkflowRunMode
from orca.modes.sim_pacing import SimPacer, current_sim_pacer

pytestmark = pytest.mark.out_of_run

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


async def _sent_speedup(
    mode: WorkflowRunMode, mock_registry: Mock, mock_conn_manager: Mock, operator_speedup: float | None = 7.0,
) -> float | None:
    mock_registry.get_device = AsyncMock(return_value=_shaker())
    mock_registry.is_device_online = AsyncMock(return_value=True)
    mock_registry.get_client_for_device = AsyncMock(return_value="client_1")
    mock_conn_manager.send_to_client = AsyncMock(return_value=True)
    controller = new_controller(mock_registry, mock_conn_manager)

    async def answer(_client_id: str, raw: str) -> bool:
        command = CommandMessage.model_validate(MessageEnvelope.model_validate_json(raw).payload)
        controller._command_futures[command.command_id].set_result(None)
        return True

    mock_conn_manager.send_to_client.side_effect = answer
    kept = KeptCommands()
    async with empty_runtime() as runtime:
        setattr(runtime, "operator_sim_pacer", lambda: SimPacer(operator_speedup))
        control = OperatorControl(runtime, "shaker_1", _shaker(), None, mode, kept, controller)
        await control.run("shake", {"speed": 500.0, "duration": 1.0}, timeout_seconds=5)
    assert kept.saved == [("shaker_1", "shake")]
    raw = mock_conn_manager.send_to_client.call_args[0][1]
    return CommandMessage.model_validate(MessageEnvelope.model_validate_json(raw).payload).sim_speedup


@pytest.mark.asyncio
async def test_an_operator_command_on_a_sim_device_waits_at_the_runtimes_operator_speedup(
    mock_registry: Mock, mock_conn_manager: Mock,
) -> None:
    assert await _sent_speedup(WorkflowRunMode.SIM, mock_registry, mock_conn_manager) == 7.0


@pytest.mark.asyncio
async def test_an_operator_command_on_a_live_device_carries_no_speedup(
    mock_registry: Mock, mock_conn_manager: Mock,
) -> None:
    assert await _sent_speedup(WorkflowRunMode.LIVE, mock_registry, mock_conn_manager) is None


@pytest.mark.asyncio
async def test_an_operator_command_under_a_run_pacer_runs_at_the_run_speedup(
    mock_registry: Mock, mock_conn_manager: Mock,
) -> None:
    token = current_sim_pacer.set(SimPacer(0.3))
    try:
        speedup = await _sent_speedup(WorkflowRunMode.SIM, mock_registry, mock_conn_manager)
    finally:
        current_sim_pacer.reset(token)
    assert speedup == 0.3
