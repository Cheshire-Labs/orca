"""An operator's pick or place by position shows on the event stream with the request the arm was sent, as a run's does."""

import pytest
from cheshire_drivers.transporter_models import PickAtCoordsRequest, PlaceAtCoordsRequest

from orca.events.execution_context import DeviceCommandContext
from orca.gateway.operator_commands import OperatorDeviceCommands
from orca.modes.run_modes import WorkflowRunMode, current_run_mode
from orca.operations.operator_device import TransporterPickPlateOperation, TransporterPlacePlateOperation
from orca.operations.operator_device_models import PickPlateRequest, PlacePlateRequest
from orca.operations.protocol import OperationError
from orca.runtime.sinks import CollectorSink
from tests.real_device_path import the_real_device_path
from tests.test_adhoc_commands_are_recorded import running_arm_bench

pytestmark = pytest.mark.asyncio


def _arm_events(sink: CollectorSink, command: str) -> list[tuple[str, DeviceCommandContext]]:
    return [
        (event.status or "", event.context) for event in sink.events
        if event.event_name.startswith("COMMAND.arm.") and isinstance(event.context, DeviceCommandContext)
        and event.context.command == command
    ]


@pytest.mark.timeout(60)
async def test_an_operator_pick_and_place_by_position_carry_the_request_the_arm_was_sent() -> None:
    async with running_arm_bench() as bench:
        sink = CollectorSink()
        path = the_real_device_path()
        path.events.add_sink(sink)
        runner = OperatorDeviceCommands(path.gateway, lambda: bench.runtime)
        token = current_run_mode.set(WorkflowRunMode.LIVE)
        try:
            await TransporterPickPlateOperation(devices=runner).run(PickPlateRequest(device_id="arm", position_id="pad"))
            await TransporterPlacePlateOperation(devices=runner).run(
                PlacePlateRequest(device_id="arm", position_id="stacker"),
            )
        finally:
            current_run_mode.reset(token)

        picks = _arm_events(sink, "pick_at_coords")
        places = _arm_events(sink, "place_at_coords")
        assert [status for status, _ in picks] == ["STARTED", "COMPLETED"]
        assert [status for status, _ in places] == ["STARTED", "COMPLETED"]
        for _, context in picks:
            assert PickAtCoordsRequest.model_validate(context.params).teachpoint.position_id == "pad"
        for _, context in places:
            assert PlaceAtCoordsRequest.model_validate(context.params).teachpoint.position_id == "stacker"


@pytest.mark.timeout(60)
async def test_a_pick_refused_before_the_arm_takes_it_still_names_the_site_the_operator_asked_for() -> None:
    async with running_arm_bench() as bench:
        sink = CollectorSink()
        path = the_real_device_path()
        path.events.add_sink(sink)
        runner = OperatorDeviceCommands(path.gateway, lambda: bench.runtime)
        arm = next(m for m in bench.runtime.system.movers if m.name == "arm")
        token = current_run_mode.set(WorkflowRunMode.LIVE)
        try:
            async with arm.lock.held_for("a run's move"):
                with pytest.raises(OperationError):
                    await TransporterPickPlateOperation(devices=runner).run(
                        PickPlateRequest(device_id="arm", position_id="pad"),
                    )
        finally:
            current_run_mode.reset(token)

        [(status, context)] = _arm_events(sink, "pick_at_coords")
        assert status == "FAILED"
        assert context.params == {"teachpoint": {"position_id": "pad"}}
