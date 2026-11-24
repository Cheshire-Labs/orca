"""An operator's pick or place at a site the arm has no teachpoint for is refused before anything is sent."""

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from orca.gateway.operator_commands import OperatorDeviceCommands
from orca.gateway.registry.snapshot import DeviceSnapshot
from orca.modes.run_modes import WorkflowRunMode, current_run_mode
from orca.operations.operator_device import TransporterPickPlateOperation, TransporterPlacePlateOperation
from orca.operations.operator_device_models import PickPlateRequest, PlacePlateRequest
from orca.operations.protocol import OperationError
from orca.resource_models.transporter import Transporter
from tests.gateway_parts import gateway_with
from tests.real_device_path import the_real_device_path
from tests.test_adhoc_commands_are_recorded import _Bench, running_arm_bench

pytestmark = pytest.mark.asyncio


def _arm(bench: _Bench) -> Transporter:
    arm = next(m for m in bench.runtime.system.movers if m.name == "arm")
    assert isinstance(arm, Transporter)
    return arm


def _commands_reaching_the_arm() -> list[str]:
    """Every command the arm's driver on the orca-client runs from here on."""
    reached: list[str] = []
    driver = the_real_device_path().simulator("arm")
    for command in ("connect", "initialize", "pick_at_coords", "place_at_coords", "move_to_coords"):
        real: Callable[..., Awaitable[object]] = getattr(driver, command)

        async def recorded(
            *, real: Callable[..., Awaitable[object]] = real, command: str = command, **request: BaseModel,
        ) -> object:
            reached.append(command)
            return await real(**request)

        setattr(driver, command, recorded)
    return reached


def _operator(bench: _Bench) -> OperatorDeviceCommands:
    snapshot = DeviceSnapshot(
        type="transporter", name="arm", interfaces=["ITransporter"], capabilities=[],
        provides_state=False, methods={}, site="test", lab="test", workcell=None,
        status="ready", last_seen=datetime.now(timezone.utc),
    )
    tracker = MagicMock()
    tracker.get_device = AsyncMock(return_value=snapshot)
    return OperatorDeviceCommands(gateway_with(tracker=tracker, controller=MagicMock()), lambda: bench.runtime)


async def _refusal(send: Callable[[], Awaitable[object]]) -> OperationError:
    token = current_run_mode.set(WorkflowRunMode.LIVE)
    try:
        with pytest.raises(OperationError) as refused:
            await send()
    finally:
        current_run_mode.reset(token)
    return refused.value


@pytest.mark.timeout(60)
@pytest.mark.parametrize("verb", ["pick", "place"])
async def test_a_pick_or_place_at_a_site_the_arm_was_not_taught_answers_teachpoint_not_found_and_sends_nothing(
    verb: str,
) -> None:
    async with running_arm_bench() as bench:
        site = "pad" if verb == "pick" else "stacker"
        store = _arm(bench).teachpoint_store
        assert await store.delete(site)
        taught = sorted(teachpoint.position_id for teachpoint in await store.list())
        reached = _commands_reaching_the_arm()
        runner = _operator(bench)

        if verb == "pick":
            refused = await _refusal(lambda: TransporterPickPlateOperation(devices=runner).run(
                PickPlateRequest(device_id="arm", position_id=site),
            ))
        else:
            refused = await _refusal(lambda: TransporterPlacePlateOperation(devices=runner).run(
                PlacePlateRequest(device_id="arm", position_id=site),
            ))

        assert (refused.http_status, refused.surface_code()) == (404, "teachpoint_not_found")
        assert refused.extras is not None
        assert refused.extras["available_teachpoints"] == taught
        assert taught, "the answer lists what the arm was taught"
        assert site in refused.message
        assert reached == []
