"""A reader's door `close` that gets no answer while an arm's place is being recorded.

The arm sets the plate down and answers. The place then tells the reader, which sends `close` as a command of its
own, and the reader never answers. Every command takes the real device path, down to a simulator whose `close`
goes silent. An operator's `close` times out at the controller. A run's is held until a person decides.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from unittest.mock import patch

import pytest
from pydantic import JsonValue
from cheshire_drivers import CartesianCoordinates as C
from cheshire_drivers import Teachpoint
from cheshire_drivers.command_timings import command_timing
from cheshire_drivers.sims import SimReaderDriver

import orca.orca as orca
from orca.devices.devices import Reader, Storage
from orca.events.event_bus import EventBus
from orca.events.execution_context import DeviceCommandContext
from orca.gateway.controller.exceptions import CommandNotSavedError, CommandOutcomeUnknownError
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models import devices as device_models
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.resource_models.unheld_command import UnheldCommand
from orca.runtime.incident_store import IncidentCategory, RecoverableTimeoutContext, SystemIncident
from orca.runtime.recoverable_timeout import RecoverableTimeoutNotHeldError
from orca.runtime.sinks import CollectorSink
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from orca.state.ops_store import OpsHistorySearchQuery
from orca.state.provenance import Provenance
from orca.state.records import HeadObservationGapDetails, ObservationGapCause, ObservationGapDetails
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.gateway._operator_command import run_operator_command
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.real_device_path import the_real_device_path
from tests.setup_package import package_stores, with_teachpoints, write_teachpoints
from tests.test_helpers import run_to_quiescence, wait_for_runtime_condition

pytestmark = pytest.mark.asyncio

WORKFLOW = "read_a_plate"
CLOSE_LIMIT_SECONDS = 0.3

_Event = tuple[str, str, str, str]
_PLACE_AND_CLOSE = {("arm", "place_at_coords"), ("reader", "close")}


class _ReaderWhoseDoorCanGoSilent(SimReaderDriver):
    """A simulated reader. While `answers` is False its `close` is taken and never answered."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.answers = True
        self.let_go = asyncio.Event()

    # The limit the in-process orca-client advertises for `close`, which the controller holds an operator's to.
    @command_timing(typical=0.0, max=CLOSE_LIMIT_SECONDS)
    async def close(self) -> None:
        if not self.answers:
            await self.let_go.wait()
        await super().close()


@dataclass
class _Bench:
    runtime: SystemRuntime
    door: _ReaderWhoseDoorCanGoSilent
    sink: CollectorSink
    plate_name: str

    def plate_site(self) -> str:
        system = self.runtime.system
        return system.labware_location_service.get(system.get_labware(self.plate_name)).position_id

    def jaws(self) -> list[str]:
        return [mover.labware.name for mover in self.runtime.system.movers if mover.labware is not None]

    def command_events(self) -> list[_Event]:
        """Each COMMAND event as (device, command, status, error code), in the order the stream carried them."""
        return [
            (
                event.context.device_name, event.context.command, event.status or "",
                (event.context.error or "").split(":", 1)[0],
            )
            for event in self.sink.events
            if event.event_name.startswith("COMMAND.") and isinstance(event.context, DeviceCommandContext)
        ]

    def place_and_close_events(self) -> list[_Event]:
        """The first place's events and the reader's `close` events, up to the place's end."""
        kept: list[_Event] = []
        for event in self.command_events():
            if event[:2] in _PLACE_AND_CLOSE:
                kept.append(event)
            if event[:2] == ("arm", "place_at_coords") and event[2] != "STARTED":
                break
        return kept

    def incident_events(self) -> int:
        return sum(1 for event in self.sink.events if event.entity_type == "INCIDENT")

    def labware_flag_events(self) -> int:
        """How many times the stream said a labware's not-observed flag was saved."""
        return sum(1 for event in self.sink.events if event.event_name == "CONTENTS.__system__.SAVED")

    async def not_observed(self) -> tuple[list[tuple[str, ObservationGapCause]], list[str]]:
        """Each labware the record flags as not observed, with the cause, and each device head it flags."""
        hits = await self.runtime.system.ops_history.store.search(OpsHistorySearchQuery())
        details = [op.details for _id, record in hits for op in record.operations]
        labware = [(gap.labware, gap.cause) for gap in details if isinstance(gap, ObservationGapDetails)]
        heads = [gap.device_name for gap in details if isinstance(gap, HeadObservationGapDetails)]
        return sorted(labware, key=lambda flagged: flagged[1].value), sorted(heads)

    async def contents_provenance(self) -> Provenance:
        plate = self.runtime.system.get_labware(self.plate_name)
        return (await self.runtime.labware.resolve_contents(plate.id)).provenance

    async def timeout_incidents(self) -> list[SystemIncident]:
        return await self.runtime.incidents.list(category=IncidentCategory.RECOVERABLE_TIMEOUT)

    def reader_fault(self) -> str | None:
        fault = the_real_device_path().gateway.controller.fault("reader")
        return None if fault is None else fault.command


@asynccontextmanager
async def _reader_bench() -> AsyncIterator[_Bench]:
    """An arm, a pad and a reader, with a plate of stated contents on the pad and a workflow that reads it."""
    stores = package_stores()
    plate = PlateTemplate("sample_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    door = _ReaderWhoseDoorCanGoSilent("reader")
    the_real_device_path().simulate_with("reader", door)
    reader = Reader("reader")
    arm = with_teachpoints(Transporter("arm"), write_teachpoints(stores, "arm", [
        Teachpoint("pad", C(200, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("reader", C(400, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("waste", C(600, 200, 300, 0, 90, 180), orientation="right"),
    ]))

    @orca.action(device=reader, inputs=[plate])
    async def read(ctx: ActionContext) -> None:
        ctx.labware("sample_plate")

    @orca.method
    async def read_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield read

    @orca.thread(labware=plate, start="pad", end="waste")
    async def plate_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield read_method

    @orca.workflow(name=WORKFLOW)
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)

    topology = Topology(
        locations={"pad": PlatePad("pad"), "reader": reader, "waste": Storage("waste")}, transporters=[arm],
    )
    build = await orca.build_system(
        name="DoorClose", workflow=workflow, topology=topology, stores=stores, event_bus=EventBus(),
        ops_history_store=memory_ops_store(),
    )
    runtime = await memory_runtime(
        build.system, event_bus=build.event_bus
    )
    await runtime.start()
    try:
        loaded = await runtime.labware.register("sample_plate", location="pad", confirm=True)
        # Stated contents give the plate something to flag: a plate nobody described has no attestation to expire.
        await runtime.labware.set_well_volumes(loaded.id, {"A1": 50.0}, reason="loaded by hand", confirm=True)
        name = next(labware.name for labware in await runtime.labware.list_all() if labware.id == loaded.id)
        sink = CollectorSink()
        the_real_device_path().events.add_sink(sink)
        runtime.register_sink(sink)
        yield _Bench(runtime, door, sink, name)
    finally:
        door.let_go.set()
        await runtime.shutdown()


def _teachpoint(position_id: str) -> dict[str, JsonValue]:
    return {"teachpoint": {"position_id": position_id}}


async def _operator_place_with_a_silent_door(bench: _Bench) -> CommandNotSavedError:
    """An operator picks the plate from the pad, then places it at the reader, whose door never answers `close`."""
    gateway = the_real_device_path().gateway
    await run_operator_command(gateway, bench.runtime, "arm", "pick_at_coords", _teachpoint("pad"))
    bench.sink.clear()
    bench.door.answers = False
    with pytest.raises(CommandNotSavedError) as raised:
        await run_operator_command(gateway, bench.runtime, "arm", "place_at_coords", _teachpoint("reader"))
    return raised.value


@asynccontextmanager
async def _reader_bench_for_a_run() -> AsyncIterator[_Bench]:
    """The bench, with a run's limit on `close` (30 seconds, from the interface) cut short while the door is silent."""
    declared = device_models.engine_time_limit
    async with _reader_bench() as bench:
        def limit(driver_cls: type, command: str) -> float | None:
            silent = command == "close" and not bench.door.answers
            return CLOSE_LIMIT_SECONDS if silent else declared(driver_cls, command)

        with patch.object(device_models, "engine_time_limit", limit):
            yield bench


async def _run_held_on_a_silent_door(bench: _Bench) -> tuple[str, SystemIncident]:
    """A run moves the plate from the pad to the reader, whose door never answers `close`: the run and its incident."""
    bench.door.answers = False
    record = await bench.runtime.submit_workflow(WORKFLOW, mode=WorkflowRunMode.CHECK_RUN)
    await wait_for_runtime_condition(bench.runtime, lambda: bench.incident_events() > 0, timeout=20)
    [incident] = await bench.timeout_incidents()
    return record.id, incident


@pytest.mark.timeout(60)
async def test_an_operators_place_sends_the_close_as_its_own_command_and_both_end_failed() -> None:
    async with _reader_bench() as bench:
        not_saved = await _operator_place_with_a_silent_door(bench)

        assert bench.place_and_close_events() == [
            ("arm", "place_at_coords", "STARTED", ""),
            ("reader", "close", "STARTED", ""),
            ("reader", "close", "FAILED", "timeout"),
            ("arm", "place_at_coords", "FAILED", "command_not_saved"),
        ]
        assert isinstance(not_saved.__cause__, CommandOutcomeUnknownError)
        [incident] = await bench.timeout_incidents()
        assert not_saved.__cause__.incident_id == incident.id


@pytest.mark.timeout(60)
async def test_an_operators_place_files_one_incident_that_names_the_reader_and_holds_no_plate_move() -> None:
    async with _reader_bench() as bench:
        await _operator_place_with_a_silent_door(bench)

        [incident] = await bench.runtime.incidents.list()
        assert incident.category is IncidentCategory.RECOVERABLE_TIMEOUT
        assert incident.execution_id is None
        assert not incident.acknowledged
        assert isinstance(incident.detail, RecoverableTimeoutContext)
        assert (incident.detail.device_id, incident.detail.command) == ("reader", "close")
        assert incident.detail.unheld == UnheldCommand(device_name="reader", motion=None, site=None)
        assert incident.message.startswith("'close' on 'reader' got no answer within its time limit")
        assert incident.message.endswith(
            "Look at the device and confirm where the plate is, then mark the command complete or abort it to "
            "settle the record."
        )


@pytest.mark.timeout(60)
async def test_an_operators_place_leaves_the_plate_at_the_reader_and_flags_only_the_plate() -> None:
    async with _reader_bench() as bench:
        await _operator_place_with_a_silent_door(bench)

        assert bench.plate_site() == "reader/slot", "where the plate is was written before the reader was told"
        assert bench.jaws() == []
        assert await bench.not_observed() == (
            [(bench.plate_name, ObservationGapCause.OPERATIONS_DROPPED)], [],
        ), "the plate standing at the reader is flagged; a door close is not a tip command, so no head is"
        assert await bench.contents_provenance() is Provenance.STALE
        assert bench.reader_fault() == "close"


@pytest.mark.timeout(60)
async def test_mark_complete_on_an_operators_incident_moves_no_record_and_clears_the_readers_fault() -> None:
    async with _reader_bench() as bench:
        await _operator_place_with_a_silent_door(bench)
        [incident] = await bench.timeout_incidents()
        flagged = await bench.not_observed()

        with pytest.raises(RecoverableTimeoutNotHeldError):
            bench.runtime.incidents.recoverable_timeout_extend(incident.id, 30.0, confirm=True)
        await bench.runtime.incidents.recoverable_timeout_mark_complete(
            incident.id, "operator", reason="the door is closed", confirm=True,
        )

        assert bench.plate_site() == "reader/slot"
        assert await bench.not_observed() == flagged, "the plate stays flagged"
        assert await bench.contents_provenance() is Provenance.STALE
        assert [settled.acknowledged for settled in await bench.runtime.incidents.list()] == [True]
        assert bench.reader_fault() is None


@pytest.mark.timeout(60)
async def test_abort_on_an_operators_incident_moves_no_record_and_leaves_the_readers_fault() -> None:
    async with _reader_bench() as bench:
        await _operator_place_with_a_silent_door(bench)
        [incident] = await bench.timeout_incidents()
        flagged = await bench.not_observed()

        await bench.runtime.incidents.recoverable_timeout_abort(
            incident.id, "operator", reason="the door is still open", confirm=True,
        )

        assert bench.plate_site() == "reader/slot"
        assert await bench.not_observed() == flagged
        assert [settled.acknowledged for settled in await bench.runtime.incidents.list()] == [True]
        assert bench.reader_fault() == "close"


@pytest.mark.timeout(60)
async def test_a_runs_place_is_held_on_the_close_with_one_incident_that_names_the_reader() -> None:
    async with _reader_bench_for_a_run() as bench:
        execution_id, incident = await _run_held_on_a_silent_door(bench)

        assert bench.place_and_close_events() == [
            ("arm", "place_at_coords", "STARTED", ""),
            ("reader", "close", "STARTED", ""),
        ], "neither has ended: the run waits for a person"
        assert incident.category is IncidentCategory.RECOVERABLE_TIMEOUT
        assert incident.execution_id == execution_id
        assert isinstance(incident.detail, RecoverableTimeoutContext)
        assert (incident.detail.device_id, incident.detail.command) == ("reader", "close")
        assert incident.detail.unheld is None
        assert incident.message.startswith("Recoverable timeout: command 'close' on device 'reader' has been running")
        assert incident.message.endswith("Execution paused; operator may extend, abort, or mark complete.")
        assert bench.plate_site() == "reader/slot"
        assert bench.jaws() == []
        assert await bench.not_observed() == ([], []), "nothing is flagged while the run is held"
        assert bench.reader_fault() is None


@pytest.mark.timeout(60)
async def test_mark_complete_on_a_runs_incident_completes_the_close_and_the_place_and_the_run_goes_on() -> None:
    async with _reader_bench_for_a_run() as bench:
        execution_id, incident = await _run_held_on_a_silent_door(bench)

        bench.door.answers = True
        await bench.runtime.incidents.recoverable_timeout_mark_complete(
            incident.id, "operator", reason="the door is closed", confirm=True,
        )
        statuses = await run_to_quiescence(bench.runtime, execution_id)

        assert bench.place_and_close_events() == [
            ("arm", "place_at_coords", "STARTED", ""),
            ("reader", "close", "STARTED", ""),
            ("reader", "close", "COMPLETED", ""),
            ("arm", "place_at_coords", "COMPLETED", ""),
        ]
        assert list(statuses.values()) == ["COMPLETED"]
        assert await bench.not_observed() == ([], []), "a person said the door closed, so nothing is flagged"
        assert [settled.acknowledged for settled in await bench.runtime.incidents.list()] == [True]
        assert bench.reader_fault() is None


@pytest.mark.timeout(60)
async def test_abort_on_a_runs_incident_fails_the_close_and_the_place_and_pauses_the_thread_at_the_move() -> None:
    async with _reader_bench_for_a_run() as bench:
        execution_id, incident = await _run_held_on_a_silent_door(bench)

        await bench.runtime.incidents.recoverable_timeout_abort(
            incident.id, "operator", reason="the door is still open", confirm=True,
        )
        # The no-answer flag, then the flag a thread's error pause writes after it pauses.
        await wait_for_runtime_condition(bench.runtime, lambda: bench.labware_flag_events() == 2, timeout=20)

        assert bench.place_and_close_events() == [
            ("arm", "place_at_coords", "STARTED", ""),
            ("reader", "close", "STARTED", ""),
            ("reader", "close", "FAILED", "unknown"),
            ("arm", "place_at_coords", "FAILED", "command_not_saved"),
        ]
        assert bench.plate_site() == "reader/slot"
        assert bench.jaws() == []
        assert await bench.not_observed() == (
            [
                (bench.plate_name, ObservationGapCause.ERROR_PAUSE),
                (bench.plate_name, ObservationGapCause.OPERATIONS_DROPPED),
            ],
            [],
        )
        incidents = await bench.runtime.incidents.list()
        assert [(filed.category, filed.acknowledged) for filed in incidents] == [
            (IncidentCategory.RECOVERABLE_TIMEOUT, True),
            (IncidentCategory.MOVE_FAILED, False),
        ], "the abort files no second timeout incident; the failed place files the move's own"
        [thread] = bench.runtime.list_threads(execution_id)
        assert (thread.status, thread.pause_reason, thread.pause_site) == ("PAUSED", "error", "MOVE")
        assert bench.reader_fault() == "close"
