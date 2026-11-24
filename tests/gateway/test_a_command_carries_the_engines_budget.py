"""Every command goes out with the seconds the engine budgets for it: its declared duration, else its typical."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from pydantic import JsonValue

from cheshire_drivers.command_timings import collect_command_timings
from cheshire_drivers.gateway_protocol import CommandMessage, MessageEnvelope
from cheshire_drivers.interfaces import IShakerDriver, ITransporterDriver
from cheshire_drivers.move_parameters import SEED_MOVE_PARAMETERS
from cheshire_drivers.shaker_models import ShakeRequest
from cheshire_drivers.sims import SimTransporterDriver
from cheshire_drivers.teachpoints import JointCoordinates, Teachpoint
from cheshire_drivers.homing_models import HomeRequest
from cheshire_drivers.transporter_models import PickAtCoordsRequest
from orca.gateway.operator_commands import OperatorControl
from orca.resource_models.reach_in import reaching_in
from tests.gateway._control_scope import KeptCommands, empty_runtime
from orca.gateway.controller import DeviceController
from tests.gateway_parts import new_controller
from orca.gateway.registry.snapshot import DeviceSnapshot
from orca.gateway.remote_transporter_driver import RemoteTransporterDriver
from orca.runtime.step_pacer import nominal_seconds_for
from orca.modes.run_modes import WorkflowRunMode
from orca.modes.sim_pacing import SimPacer, current_sim_pacer

NEST = JointCoordinates(base=40.0, shoulder=60.0, elbow=120.0, wrist=-90.0)


@pytest.fixture
def mock_registry() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_conn_manager() -> MagicMock:
    return MagicMock()



def test_a_pick_and_a_place_each_budget_their_own_typical() -> None:
    timings = collect_command_timings(ITransporterDriver)

    assert nominal_seconds_for(SimTransporterDriver, "pick_at_coords", {}) == timings["pick_at_coords"].typical_seconds
    assert nominal_seconds_for(SimTransporterDriver, "place_at_coords", {}) == timings["place_at_coords"].typical_seconds


def test_every_other_command_gets_the_engines_own_step_nominal() -> None:
    home_typical = collect_command_timings(ITransporterDriver)["home"].typical_seconds

    assert nominal_seconds_for(ITransporterDriver, "home", {"request": HomeRequest()}) == home_typical
    assert nominal_seconds_for(IShakerDriver, "shake", {"request": ShakeRequest(speed=500, duration=42.0)}) == 42.0


# -- on the wire --

def _arm() -> DeviceSnapshot:
    return DeviceSnapshot(
        type="transporter", name="pf400_1", interfaces=["ITransporter"], capabilities=[],
        provides_state=False, methods={}, site="test", lab="test", workcell=None,
        status="ready", last_seen=datetime.utcnow(),
    )


def _answering(controller: DeviceController, mock_registry: Mock, mock_conn_manager: Mock) -> None:
    mock_registry.get_device = AsyncMock(return_value=_arm())
    mock_registry.is_device_online = AsyncMock(return_value=True)
    mock_registry.get_client_for_device = AsyncMock(return_value="client_1")

    async def answer(_client_id: str, raw: str) -> bool:
        command = CommandMessage.model_validate(MessageEnvelope.model_validate_json(raw).payload)
        controller._command_futures[command.command_id].set_result(None)
        return True

    mock_conn_manager.send_to_client = AsyncMock(side_effect=answer)


def _pick_params() -> dict[str, JsonValue]:
    return RemoteTransporterDriver._coords_payload(PickAtCoordsRequest(
        teachpoint=Teachpoint(position_id="pad_1", coordinates=NEST), handling=SEED_MOVE_PARAMETERS,
    ))


def _sent(mock_conn_manager: Mock) -> CommandMessage:
    raw = mock_conn_manager.send_to_client.call_args[0][1]
    return CommandMessage.model_validate(MessageEnvelope.model_validate_json(raw).payload)


@pytest.mark.asyncio
async def test_a_command_under_a_run_pacer_carries_its_budget(mock_registry: Mock, mock_conn_manager: Mock) -> None:
    controller = new_controller(mock_registry, mock_conn_manager)
    _answering(controller, mock_registry, mock_conn_manager)
    token = current_sim_pacer.set(SimPacer(0.1))
    try:
        with reaching_in("pf400_1"):
            await controller.execute_command(
                "pf400_1", "pick_at_coords", _pick_params(),
                timeout_seconds=30, effective_mode=WorkflowRunMode.SIM, execution_id="exec_1",
            )
    finally:
        current_sim_pacer.reset(token)

    sent = _sent(mock_conn_manager)
    assert (sent.sim_speedup, sent.nominal_seconds) == (0.1, nominal_seconds_for(ITransporterDriver, "pick_at_coords", {}))


@pytest.mark.asyncio
async def test_a_live_command_carries_its_budget_and_no_scale(mock_registry: Mock, mock_conn_manager: Mock) -> None:
    controller = new_controller(mock_registry, mock_conn_manager)
    _answering(controller, mock_registry, mock_conn_manager)

    with reaching_in("pf400_1"):
        await controller.execute_command(
            "pf400_1", "pick_at_coords", _pick_params(), timeout_seconds=30, effective_mode=WorkflowRunMode.LIVE, execution_id="exec_1",
        )

    # Hardware paces itself and ignores the budget; a client-built sim standing in for it spends it.
    sent = _sent(mock_conn_manager)
    assert (sent.sim_speedup, sent.nominal_seconds) == (None, nominal_seconds_for(ITransporterDriver, "pick_at_coords", {}))


@pytest.mark.asyncio
async def test_an_operator_command_on_a_sim_arm_carries_the_engines_budget_at_the_operator_speedup(
    mock_registry: Mock, mock_conn_manager: Mock,
) -> None:
    """An operator's pick goes through Transporter.pick, the run's own path; any other arm command keeps the same budget."""
    controller = new_controller(mock_registry, mock_conn_manager)
    _answering(controller, mock_registry, mock_conn_manager)

    async with empty_runtime() as runtime:
        setattr(runtime, "operator_sim_pacer", lambda: SimPacer(7.0))
        await OperatorControl(runtime, "pf400_1", _arm(), None, WorkflowRunMode.SIM, KeptCommands(), controller).run(
            "home", {}, timeout_seconds=5,
        )

    sent = _sent(mock_conn_manager)
    assert (sent.sim_speedup, sent.nominal_seconds) == (7.0, nominal_seconds_for(ITransporterDriver, "home", {}))
