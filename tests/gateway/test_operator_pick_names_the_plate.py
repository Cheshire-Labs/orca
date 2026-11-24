"""An operator's pick reaches the arm as a run's pick does: naming the plate the ledger has at the site, with the sim's checks on."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from cheshire_drivers.transporter_models import PickAtCoordsRequest
from pydantic import JsonValue

from orca.resource_models.transporter import Transporter
from orca.modes.run_modes import WorkflowRunMode, current_run_mode
from tests.gateway._operator_command import run_operator_command
from tests.gateway_parts import gateway_with
from tests.test_adhoc_commands_are_recorded import _Bench, _teachpoint, arm_bench

__all__ = ["arm_bench"]


async def _sent_request(bench: _Bench, params: dict[str, Any]) -> PickAtCoordsRequest:
    sent: list[PickAtCoordsRequest] = []
    arm = next(m for m in bench.runtime.system.movers if m.name == "arm")
    assert isinstance(arm, Transporter)
    for driver in [arm.driver]:
        async def pick_at_coords(request: PickAtCoordsRequest) -> None:
            sent.append(request)

        setattr(driver, "pick_at_coords", pick_at_coords)

    tracker_snapshot = MagicMock()
    tracker_snapshot.interfaces = ["ITransporter"]
    token = current_run_mode.set(WorkflowRunMode.LIVE)
    try:
        tracker = MagicMock()
        tracker.get_device = AsyncMock(return_value=tracker_snapshot)
        controller = MagicMock()
        controller.execute_command = AsyncMock(return_value=None)
        await run_operator_command(
            gateway_with(tracker=tracker, controller=controller), bench.runtime, "arm", "pick_at_coords", params,
        )
    finally:
        current_run_mode.reset(token)
    (only,) = sent
    return only


@pytest.mark.timeout(30)
async def test_an_operator_pick_at_a_site_holding_a_plate_sends_that_plates_identity(arm_bench: _Bench) -> None:
    request = await _sent_request(arm_bench, _teachpoint("pad"))

    assert request.expected_labware is not None
    assert request.expected_labware.labware_id == arm_bench.labware_id
