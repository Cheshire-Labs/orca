"""A command the device failed after taking it flags the labware it names, and the head for a tip command, and asks a person.

The device may have moved before it failed, so the record cannot stay marked known. A command refused before anything
moved changes nothing, so it flags nothing.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from unittest.mock import Mock

import pytest

from orca.gateway.controller.command_pipeline import NoAnswer
from orca.runtime.system_runtime import _outcome_unknown_message
from pydantic import JsonValue
from cheshire_drivers.driver_errors import DriverRefusedError
from cheshire_drivers.liquid_handler_models import AspirateRequest, LabwareStateResponse

from orca.gateway.controller.command_events import plain_error
from orca.gateway.controller.command_pipeline import DeviceCommandPipeline, NoAnswer, PartialChannelFailureError
from orca.gateway.controller.exceptions import CommandExecutionError, DeviceOfflineError
from orca.gateway.operator_commands import OperatorDeviceCommands
from orca.resource_models.command_effects import (
    Carried, CommandStatement, LabwareMotion, SiteEvent, SiteNotice, stated_after_a_failure,
)
from orca.resource_models.device_error import TipsNotPresentError
from orca.runtime.incident_store import IncidentCategory
from orca.runtime.server_events import ServerEvents
from orca.runtime.system_runtime import SystemRuntime
from orca.state.command_operations import ChannelFailureNotReadError
from orca.state.ops_store import OpsHistorySearchQuery
from orca.state.provenance import Provenance
from orca.state.records import HeadObservationGapDetails, ObservationGapCause, ObservationGapDetails
from tests.memory_activity import memory_runtime
from tests.real_device_path import the_real_device_path
from tests.recording_handler import RecordingHandler
from tests.test_deck_resident_reagent_pipetting import build_pipetting_bench

pytestmark = pytest.mark.timeout(60)

TIPS_SITE = "lh/carrier-15-0"
RESERVOIR_SITE = "lh/carrier-25-0"


class _AspirateFails(RecordingHandler):
    """A handler whose aspirate takes the command and then raises `error`."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def aspirate(self, request: AspirateRequest) -> LabwareStateResponse:
        raise self._error


class _Bench:
    def __init__(self, runtime: SystemRuntime, reservoir: str, tips: str, tips_id: str) -> None:
        self.runtime = runtime
        self.reservoir = reservoir
        self.tips = tips
        self.tips_id = tips_id

    async def send(self, command: str, params: dict[str, JsonValue]) -> None:
        runner = OperatorDeviceCommands(the_real_device_path().gateway, lambda: self.runtime)
        await runner.run_named("lh", command, dict(params))

    async def aspirate(self) -> None:
        await self.send("aspirate", {"aspirations": [{"labware": self.reservoir, "positions": ["A1"], "volumes": [5.0]}]})

    async def flagged(self) -> set[str]:
        """Each labware a cut-off flag stands on, and "head" for the handler's head."""
        found: set[str] = set()
        for labware in self.runtime.system.labwares:
            gaps = [op.details.cause for op in await labware.ops() if isinstance(op.details, ObservationGapDetails)]
            if ObservationGapCause.OPERATIONS_DROPPED in gaps:
                found.add(labware.name)
        hits = await self.runtime.system.ops_history.store.search(OpsHistorySearchQuery())
        if any(
            isinstance(op.details, HeadObservationGapDetails) and op.details.device_name == "lh"
            and op.details.cause is ObservationGapCause.OPERATIONS_DROPPED
            for _id, record in hits for op in record.operations
        ):
            found.add("head")
        return found

    async def incident_messages(self) -> list[str]:
        incidents = await self.runtime.incidents.list(category=IncidentCategory.RECOVERABLE_TIMEOUT)
        return [incident.message for incident in incidents]


@asynccontextmanager
async def _handler_whose_aspirate_raises(error: Exception) -> AsyncIterator[_Bench]:
    build, _lh = await build_pipetting_bench(_AspirateFails(error), reservoir_volume=1000.0)
    runtime = await memory_runtime(
        build.system, event_bus=build.event_bus
    )
    await runtime.start()
    try:
        tips = await runtime.labware.register("tips", location=TIPS_SITE, confirm=True)
        reservoir = await runtime.labware.register("reservoir", location=RESERVOIR_SITE, confirm=True)
        names = {labware.id: labware.name for labware in await runtime.labware.list_all()}
        yield _Bench(runtime, names[reservoir.id], names[tips.id], tips.id)
    finally:
        await runtime.shutdown()


async def test_an_aspirate_the_device_failed_part_way_flags_its_plate_and_leaves_the_head_known() -> None:
    async with _handler_whose_aspirate_raises(RuntimeError("pressure error on channel 1")) as bench:
        await bench.runtime.devices.set_mounted_tips("lh", {0: (bench.tips, "A1")}, reason="bench setup", confirm=True)
        assert (await bench.runtime.devices.get_mounted_tips("lh")).provenance is Provenance.KNOWN

        with pytest.raises(Exception, match="pressure error"):
            await bench.aspirate()

        assert await bench.flagged() == {bench.reservoir}, "the tip rack was not named, so it stays as it was"
        assert (await bench.runtime.devices.get_mounted_tips("lh")).provenance is Provenance.KNOWN
        messages = await bench.incident_messages()
        assert len(messages) == 1
        assert "failed after the device took it" in messages[0]
        assert "state what the labware it used now hold" in messages[0], "an aspirate leaves the head known"


@pytest.mark.parametrize("command, names_the_head", [("discard_tips", True), ("pick_up_tips96", True), ("aspirate", False)])
def test_the_incident_asks_about_the_head_only_for_a_tip_command(command: str, names_the_head: bool) -> None:
    message = _outcome_unknown_message("lh", command, 0.0, NoAnswer.FAILED)

    assert ("its head" in message) is names_the_head


async def test_a_command_the_driver_refused_flags_nothing() -> None:
    async with _handler_whose_aspirate_raises(DriverRefusedError("the head is busy")) as bench:
        with pytest.raises(Exception, match="the head is busy"):
            await bench.aspirate()

        assert await bench.flagged() == set()
        assert await bench.incident_messages() == []


async def test_a_tip_pick_up_refused_before_it_is_sent_flags_nothing() -> None:
    async with _handler_whose_aspirate_raises(RuntimeError("unused")) as bench:
        await bench.runtime.labware.set_tip_state(bench.tips_id, ["B1"], reason="bench setup", confirm=True)

        with pytest.raises(Exception) as refused:
            await bench.send("pick_up_tips", {"picks": [{"tip_rack": bench.tips, "positions": ["A1"]}]})

        assert isinstance(refused.value.__cause__ or refused.value, TipsNotPresentError)
        assert await bench.flagged() == set()
        assert await bench.incident_messages() == []


async def test_a_pick_up_onto_a_channel_that_already_holds_a_tip_flags_nothing() -> None:
    """The driver's own check stops it before the handler is asked, and the driver says so: nothing moved."""
    async with _handler_whose_aspirate_raises(RuntimeError("unused")) as bench:
        await bench.send("pick_up_tips", {"picks": [{"tip_rack": bench.tips, "positions": ["A1"]}]})

        with pytest.raises(Exception, match="has tip"):
            await bench.send("pick_up_tips", {"picks": [{"tip_rack": bench.tips, "positions": ["B1"]}]})

        assert await bench.flagged() == set()
        assert await bench.incident_messages() == []
        assert bench.runtime.device_faults.fault("lh") is None


async def test_a_command_naming_labware_the_driver_deck_lacks_flags_nothing() -> None:
    async with _handler_whose_aspirate_raises(RuntimeError("unused")) as bench:
        with pytest.raises(Exception, match="not found"):
            await bench.send(
                "dispense", {"dispenses": [{"labware": "no_such_plate", "positions": ["A1"], "volumes": [5.0]}]},
            )

        assert await bench.flagged() == set()
        assert await bench.incident_messages() == []
        assert bench.runtime.device_faults.fault("lh") is None


class _Hears:
    def __init__(self) -> None:
        self.heard: list[tuple[str, NoAnswer, tuple[str, ...]]] = []

    async def no_answer(
        self, device_name: str, command: str, command_id: str, execution_id: str | None,
        seconds: float, why: NoAnswer, statement: CommandStatement | None, named: Sequence[str],
    ) -> str | None:
        self.heard.append((device_name, why, tuple(named)))
        return "incident-1"


async def _nothing_saved(_answer: None) -> None:
    return None


async def _send_that_raises(error: Exception) -> list[tuple[str, NoAnswer, tuple[str, ...]]]:
    pipeline: DeviceCommandPipeline[CommandStatement] = DeviceCommandPipeline(())
    heard = _Hears()
    pipeline.report_to(ServerEvents(), heard, plain_error)

    async def send() -> None:
        raise error

    with pytest.raises(type(error)):
        await pipeline.run_device_command(
            "lh", "aspirate", send, _nothing_saved, checks=(), execution_id="run_1", named=["plate_a"], params=None,
        )
    return heard.heard


async def test_a_run_command_that_may_have_moved_is_reported_with_the_labware_it_names() -> None:
    failed = CommandExecutionError("pressure error", "STARFirmwareError")
    failed.may_have_moved = True

    assert await _send_that_raises(failed) == [("lh", NoAnswer.FAILED, ("plate_a",))]


async def test_a_run_command_that_never_reached_the_device_is_not_reported() -> None:
    assert await _send_that_raises(DeviceOfflineError("lh has no connected client")) == []


def test_mark_complete_applies_nothing_once_a_failed_place_put_the_plate_back() -> None:
    """A mover that carries the plate in its one place call: a failed place records the plate back on its source,
    so the place's completed effect, which starts from the jaws, no longer applies."""
    plate, jaws, site, source = Mock(), Mock(), Mock(), Mock()
    put_back = CommandStatement(
        "arm", LabwareMotion(plate, jaws, site, Carried.SET_DOWN, failed_back_to=source),
        SiteNotice(site, plate, Mock(), SiteEvent.PLACED),
    )
    stays = CommandStatement("arm", LabwareMotion(plate, site, jaws, Carried.PICKED_UP, failed_back_to=None), None)

    assert stated_after_a_failure(put_back) == CommandStatement("arm", None, None)
    assert stated_after_a_failure(stays) is stays


async def test_a_failed_channel_with_no_row_is_reported_like_a_failed_command() -> None:
    """A discard that names no channels and reports a failed channel saves nothing, so nobody knows what the head
    carries: it is flagged and a person is asked."""
    pipeline: DeviceCommandPipeline[CommandStatement] = DeviceCommandPipeline(())
    heard = _Hears()
    pipeline.report_to(ServerEvents(), heard, plain_error)

    async def answered() -> None:
        return None

    async def nothing_can_read_it(_answer: None) -> None:
        raise ChannelFailureNotReadError("discard_tips")

    with pytest.raises(PartialChannelFailureError):
        await pipeline.run_device_command(
            "lh", "discard_tips", answered, nothing_can_read_it, checks=(), execution_id="run_1", named=(), params=None,
        )

    assert heard.heard == [("lh", NoAnswer.FAILED, ())]


async def test_a_failed_channel_whose_rows_were_saved_is_not_reported() -> None:
    pipeline: DeviceCommandPipeline[CommandStatement] = DeviceCommandPipeline(())
    heard = _Hears()
    pipeline.report_to(ServerEvents(), heard, plain_error)

    async def answered() -> None:
        return None

    async def rows_were_saved(_answer: None) -> None:
        raise PartialChannelFailureError([Mock()])

    with pytest.raises(PartialChannelFailureError):
        await pipeline.run_device_command(
            "lh", "aspirate", answered, rows_were_saved, checks=(), execution_id="run_1", named=["plate_a"], params=None,
        )

    assert heard.heard == []
