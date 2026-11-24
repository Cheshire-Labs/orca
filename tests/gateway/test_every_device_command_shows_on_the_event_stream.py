"""Every device command runs in a `one_command` block, which sends one COMMAND event when the device takes the command and one after its records."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from cheshire_drivers.driver_errors import InstrumentOutcome
from cheshire_drivers.gateway_protocol import CommandMessage, MessageEnvelope, ResponseMessage
from pydantic import JsonValue

from orca.events.execution_context import DeviceCommandContext
from orca.events.runtime_event import RuntimeEvent
from orca.gateway.controller import DeviceController
from orca.gateway.controller.command_events import one_command
from orca.gateway.controller.command_pipeline import DeviceCommandPipeline
from orca.modes.sim_pacing import SimPacer, current_sim_pacer
from orca.modes.run_modes import RunningThread, current_execution_id, current_thread
from orca.gateway.controller.exceptions import CommandExecutionError, CommandNotSavedError, DeviceLockedError
from orca.gateway.operator_commands import OperatorControl, OperatorDeviceCommands
from orca.operations.error_mapping import error_label
from orca.gateway.registry.snapshot import DeviceSnapshot
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.server_events import ServerEvents
from orca.runtime.sinks import CollectorSink
from tests.gateway._control_scope import stub_control_scope
from tests.gateway_parts import gateway_with, new_controller
from tests.test_helpers import wait_until

pytestmark = pytest.mark.asyncio


class _BrokenSink:
    def on_event(self, event: RuntimeEvent) -> None:
        raise RuntimeError("the socket went away")


def _connected() -> tuple[MagicMock, MagicMock]:
    tracker, connections = MagicMock(), MagicMock()
    tracker.get_device = AsyncMock(return_value=DeviceSnapshot(
        type="shaker", name="shaker_1", interfaces=["IShaker"], capabilities=[],
        provides_state=False, methods={}, site="test", lab="test", workcell=None,
        status="ready", last_seen=datetime.utcnow(),
    ))
    tracker.is_device_online = AsyncMock(return_value=True)
    tracker.get_client_for_device = AsyncMock(return_value="client_1")
    connections.send_to_client = AsyncMock(return_value=True)
    return tracker, connections


def _controller(*sinks: CollectorSink | _BrokenSink) -> tuple[DeviceController, MagicMock]:
    events = ServerEvents()
    for sink in sinks:
        events.add_sink(sink)
    tracker, connections = _connected()
    return new_controller(tracker, connections, events=events), connections


async def _answer(controller: DeviceController, connections: MagicMock, response: ResponseMessage) -> None:
    await wait_until(lambda: connections.send_to_client.called)
    envelope = MessageEnvelope.model_validate_json(connections.send_to_client.call_args[0][1])
    sent = CommandMessage.model_validate(envelope.payload)
    await controller.handle_response(response.model_copy(update={"command_id": sent.command_id}))


async def _nothing_to_save() -> None:
    return None


def _shake(
    controller: DeviceController, execution_id: str | None = None,
    save: Callable[[], Awaitable[None]] = _nothing_to_save,
) -> asyncio.Task[JsonValue]:
    """A shake the engine sends, as a move, a deck push or a bring-up sends its commands."""
    async def run() -> JsonValue:
        token = current_execution_id.set(execution_id)
        try:
            return await DeviceCommandPipeline(()).run_engine_command(
                "shaker_1", "shake",
                lambda: controller.execute_command(
                    "shaker_1", "shake", {"speed": 500.0, "duration": 1.0},
                    timeout_seconds=30, effective_mode=WorkflowRunMode.SIM, execution_id=execution_id,
                ),
                save, carries_out=None,
            )
        finally:
            current_execution_id.reset(token)
    return asyncio.create_task(run())


def _contexts(sink: CollectorSink) -> list[DeviceCommandContext]:
    return [e.context for e in sink.events if isinstance(e.context, DeviceCommandContext)]


async def test_a_command_sends_started_then_completed_under_the_wire_command_id() -> None:
    sink = CollectorSink()
    controller, connections = _controller(sink)

    task = _shake(controller)
    await _answer(controller, connections, ResponseMessage(command_id="", success=True, result={"ok": True}))
    await task

    envelope = MessageEnvelope.model_validate_json(connections.send_to_client.call_args[0][1])
    wire_id = CommandMessage.model_validate(envelope.payload).command_id
    assert [e.event_name for e in sink.events] == ["COMMAND.shaker_1.STARTED", "COMMAND.shaker_1.COMPLETED"]
    assert [c.command_id for c in _contexts(sink)] == [wire_id, wire_id]
    assert _contexts(sink)[1].result == {"ok": True}


async def test_a_failed_command_says_what_it_left_the_instrument_in() -> None:
    sink = CollectorSink()
    controller, connections = _controller(sink)

    task = _shake(controller)
    await _answer(controller, connections, ResponseMessage(
        command_id="", success=False, error="jammed", error_type="DriverError",
        instrument_outcome=InstrumentOutcome.FAILED,
    ))
    with pytest.raises(CommandExecutionError):
        await task

    finished = sink.events[-1]
    assert finished.status == "FAILED"
    assert isinstance(finished.context, DeviceCommandContext) and finished.context.outcome == "failed"


async def test_a_run_command_belongs_to_its_run() -> None:
    sink = CollectorSink()
    controller, connections = _controller(sink)

    task = _shake(controller, execution_id="exec_1")
    await _answer(controller, connections, ResponseMessage(command_id="", success=True, result=None))
    await task

    assert {e.execution_id for e in sink.events} == {"exec_1"}


async def test_every_event_of_a_run_command_names_the_thread_that_sent_it() -> None:
    sink = CollectorSink()
    controller, connections = _controller(sink)
    token = current_thread.set(RunningThread(id="thread_1", name="plate_1_journey"))
    try:
        task = _shake(controller, execution_id="exec_1")
    finally:
        current_thread.reset(token)
    await _answer(controller, connections, ResponseMessage(command_id="", success=True, result=None))
    await task

    assert [(e.status, c.thread_id, c.thread_name) for e, c in zip(sink.events, _contexts(sink))] == [
        ("STARTED", "thread_1", "plate_1_journey"), ("COMPLETED", "thread_1", "plate_1_journey"),
    ]


async def test_a_command_no_thread_sent_names_no_thread() -> None:
    sink = CollectorSink()
    controller, connections = _controller(sink)

    async with one_command("shaker_1", "pick_at_coords", {"teachpoint": "pad"}, error_text=error_label):
        await _reach_in(controller, connections)

    assert [(c.thread_id, c.thread_name) for c in _contexts(sink)] == [(None, None), (None, None)]


async def test_a_sink_that_fails_never_fails_the_command() -> None:
    controller, connections = _controller(_BrokenSink())

    task = _shake(controller)
    await _answer(controller, connections, ResponseMessage(command_id="", success=True, result=None))

    assert await task is None


async def test_the_end_event_goes_out_after_the_save() -> None:
    sink = CollectorSink()
    controller, connections = _controller(sink)
    seen_at_save: list[list[str]] = []

    async def save() -> None:
        seen_at_save.append([e.status for e in sink.events])

    task = _shake(controller, save=save)
    await _answer(controller, connections, ResponseMessage(command_id="", success=True, result=None))
    await task

    assert seen_at_save == [["STARTED"]]
    assert [e.status for e in sink.events] == ["STARTED", "COMPLETED"]


async def test_an_engine_command_whose_save_fails_ends_failed_and_says_it_ran() -> None:
    sink = CollectorSink()
    controller, connections = _controller(sink)

    async def save() -> None:
        raise OSError("disk full")

    task = _shake(controller, save=save)
    await _answer(controller, connections, ResponseMessage(command_id="", success=True, result=None))
    with pytest.raises(CommandNotSavedError, match="'shake' ran on 'shaker_1'.*disk full.*do not send it again"):
        await task

    assert [e.status for e in sink.events] == ["STARTED", "FAILED"]
    finished = sink.events[-1].context
    assert isinstance(finished, DeviceCommandContext) and finished.error_type == "CommandNotSavedError"


async def _reach_in(controller: DeviceController, connections: MagicMock) -> None:
    for _ in range(2):
        sent = asyncio.create_task(controller.execute_command(
            "shaker_1", "shake", {"speed": 500.0, "duration": 1.0}, timeout_seconds=30, effective_mode=WorkflowRunMode.SIM,
        ))
        await _answer(controller, connections, ResponseMessage(command_id="", success=True, result=None))
        await sent
        connections.send_to_client.reset_mock()


async def test_an_operator_command_shows_as_it_was_sent_not_as_the_calls_it_made() -> None:
    sink = CollectorSink()
    controller, connections = _controller(sink)

    async with one_command("shaker_1", "pick_at_coords", {"teachpoint": "pad"}, error_text=error_label):
        await _reach_in(controller, connections)

    assert [(e.status, e.context.command) for e in sink.events if isinstance(e.context, DeviceCommandContext)] == [
        ("STARTED", "pick_at_coords"), ("COMPLETED", "pick_at_coords"),
    ]


async def test_a_command_whose_save_fails_ends_failed_with_command_not_saved() -> None:
    sink = CollectorSink()
    controller, connections = _controller(sink)

    with pytest.raises(CommandNotSavedError):
        async with one_command("shaker_1", "shake", {}, error_text=error_label):
            task = _shake(controller)
            await _answer(controller, connections, ResponseMessage(command_id="", success=True, result=None))
            await task
            raise CommandNotSavedError("shake ran on shaker_1, but saving it failed")

    assert [e.status for e in sink.events] == ["STARTED", "FAILED"]
    finished = sink.events[-1].context
    assert isinstance(finished, DeviceCommandContext) and str(finished.error).startswith("command_not_saved:")


async def test_an_operator_command_refused_before_the_device_shows_failed_with_its_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_control_scope(monkeypatch)
    monkeypatch.setattr(OperatorControl, "run", AsyncMock(side_effect=DeviceLockedError("held by a shake")))
    sink = CollectorSink()
    events = ServerEvents()
    events.add_sink(sink)
    controller = MagicMock()
    controller.command_events = events
    runner = OperatorDeviceCommands(gateway_with(controller=controller), lambda: None)

    with pytest.raises(DeviceLockedError):
        await runner.run_named("shaker_1", "shake", {"speed": 500})

    assert [e.status for e in sink.events] == ["FAILED"]
    failed = sink.events[0].context
    assert isinstance(failed, DeviceCommandContext) and str(failed.error).startswith("device_locked:")
    assert (failed.thread_id, failed.thread_name) == (None, None)


async def test_a_command_refused_by_a_busy_device_sends_no_started() -> None:
    """STARTED means the device took the command. One the controller refuses because another holds the device ends FAILED alone."""
    sink = CollectorSink()
    controller, connections = _controller(sink)
    first = _shake(controller)
    await wait_until(lambda: connections.send_to_client.called)

    with pytest.raises(DeviceLockedError):
        await _shake(controller)
    await _answer(controller, connections, ResponseMessage(command_id="", success=True, result=None))
    await first

    assert [e.status for e in sink.events] == ["STARTED", "FAILED", "COMPLETED"]


async def test_a_command_queued_behind_another_on_the_same_device_sends_no_started_until_the_first_finishes() -> None:
    """A command waiting for a device it shares with another is not started: STARTED goes out once the device takes it."""
    sink = CollectorSink()
    controller, connections = _controller(sink)
    device_lock = asyncio.Lock()

    async def queued_shake() -> JsonValue:
        async with device_lock:
            return await _shake(controller)

    first = asyncio.create_task(queued_shake())
    await wait_until(lambda: connections.send_to_client.call_count == 1)
    second = asyncio.create_task(queued_shake())
    await asyncio.sleep(0)

    assert [e.status for e in sink.events] == ["STARTED"]
    await _answer(controller, connections, ResponseMessage(command_id="", success=True, result=None))
    await first
    await wait_until(lambda: connections.send_to_client.call_count == 2)
    assert [e.status for e in sink.events] == ["STARTED", "COMPLETED", "STARTED"]
    await _answer(controller, connections, ResponseMessage(command_id="", success=True, result=None))
    await second
    assert [e.status for e in sink.events] == ["STARTED", "COMPLETED", "STARTED", "COMPLETED"]


async def test_a_completed_command_carries_its_params_and_how_long_it_took() -> None:
    sink = CollectorSink()
    controller, connections = _controller(sink)

    task = _shake(controller)
    await _answer(controller, connections, ResponseMessage(command_id="", success=True, result={"ok": True}))
    await task

    completed = _contexts(sink)[-1]
    assert completed.params == {"speed": 500.0, "duration": 1.0}
    assert completed.duration_ms is not None and completed.duration_ms >= 0.0


async def test_a_failed_commands_event_carries_no_result() -> None:
    sink = CollectorSink()
    controller, connections = _controller(sink)

    task = _shake(controller)
    await _answer(controller, connections, ResponseMessage(
        command_id="", success=False, error="jammed", error_type="DriverError",
        instrument_outcome=InstrumentOutcome.FAILED,
    ))
    with pytest.raises(CommandExecutionError):
        await task

    failed = _contexts(sink)[-1]
    assert failed.result is None


async def test_a_command_sent_while_another_runs_has_its_own_events_and_the_outer_keeps_its_own_params() -> None:
    """An arm's pick opens the site's door first: the door's `open` is its own command, and the pick's STARTED carries the pick."""
    sink = CollectorSink()
    controller, connections = _controller(sink)
    pipeline: DeviceCommandPipeline[None] = DeviceCommandPipeline(())

    async def send(device: str, command: str, params: dict[str, JsonValue]) -> JsonValue:
        return await controller.execute_command(
            device, command, params, timeout_seconds=30, effective_mode=WorkflowRunMode.SIM,
        )

    async def pick_through_the_door() -> JsonValue:
        await pipeline.run_engine_command(
            "reader_1", "open", lambda: send("reader_1", "open", {}), _nothing_to_save, carries_out=None,
        )
        return await send("shaker_1", "shake", {"speed": 500.0, "duration": 1.0})

    task = asyncio.create_task(
        pipeline.run_engine_command("shaker_1", "shake", pick_through_the_door, _nothing_to_save, carries_out=None),
    )
    await _answer(controller, connections, ResponseMessage(command_id="", success=True, result=None))
    connections.send_to_client.reset_mock()
    await _answer(controller, connections, ResponseMessage(command_id="", success=True, result=None))
    await task

    assert [(c.device_name, c.command, e.status, c.params) for e, c in zip(sink.events, _contexts(sink))] == [
        ("reader_1", "open", "STARTED", {}),
        ("reader_1", "open", "COMPLETED", {}),
        ("shaker_1", "shake", "STARTED", {"speed": 500.0, "duration": 1.0}),
        ("shaker_1", "shake", "COMPLETED", {"speed": 500.0, "duration": 1.0}),
    ]


class _Records:
    """An effect that keeps each stage it is asked to write, with what the command stated."""

    runs_after_a_failed_save = False

    def __init__(self) -> None:
        self.written: list[tuple[str, str]] = []

    async def completed(self, statement: str) -> None:
        self.written.append(("completed", statement))

    async def failed(self, statement: str) -> None:
        self.written.append(("failed", statement))

    async def no_answer(self, statement: str) -> None:
        self.written.append(("no_answer", statement))


async def _saved(_result: None) -> None:
    return None


async def test_a_door_opened_inside_a_place_writes_no_labware_record_of_its_own() -> None:
    """The place writes where the plate went, once. The door's own command states no labware, so it writes nothing."""
    records = _Records()
    pipeline: DeviceCommandPipeline[str] = DeviceCommandPipeline((records,))

    async def nothing() -> None:
        return None

    async def place_through_the_door() -> None:
        await pipeline.run_engine_command("reader_1", "open", nothing, _nothing_to_save, carries_out=None)
        await pipeline.run_engine_command("reader_1", "close", nothing, _nothing_to_save, carries_out=None)

    await pipeline.run(
        "arm_1", "place_at_coords", place_through_the_door, _saved, statement="the plate is on the reader",
        checks=(), params={}, execution_id=None, events=None, carries_out=None,
    )

    assert records.written == [("completed", "the plate is on the reader")]


async def test_started_says_how_fast_the_senders_run_goes() -> None:
    """A watcher reads the run's speed-up off STARTED, so it knows how long the device will wait."""
    sink = CollectorSink()
    controller, connections = _controller(sink)
    token = current_sim_pacer.set(SimPacer(7.0))
    try:
        task = _shake(controller)
        await _answer(controller, connections, ResponseMessage(command_id="", success=True, result=None))
        await task
    finally:
        current_sim_pacer.reset(token)

    assert [c.sim_speedup for c in _contexts(sink)] == [7.0, 7.0]


async def test_started_says_how_long_the_engine_budgets_for_the_command() -> None:
    """A one-second shake is budgeted one second, so a watcher can fill that time."""
    sink = CollectorSink()
    controller, connections = _controller(sink)

    task = _shake(controller)
    await _answer(controller, connections, ResponseMessage(command_id="", success=True, result=None))
    await task

    assert [c.nominal_seconds for c in _contexts(sink)] == [1.0, 1.0]


async def test_a_command_no_run_paces_states_no_speed_up() -> None:
    sink = CollectorSink()
    controller, connections = _controller(sink)

    task = _shake(controller)
    await _answer(controller, connections, ResponseMessage(command_id="", success=True, result=None))
    await task

    assert [c.sim_speedup for c in _contexts(sink)] == [None, None]


async def _sent_speedup(mode: WorkflowRunMode, default_sim_speedup: float | None) -> float | None:
    """The speed-up a shake in `mode` carries to orca-client when no run paces it."""
    tracker, connections = _connected()
    controller = new_controller(tracker, connections, default_sim_speedup=default_sim_speedup)
    task = asyncio.create_task(controller.execute_command(
        "shaker_1", "shake", {"speed": 500.0, "duration": 1.0}, timeout_seconds=30, effective_mode=mode,
    ))
    await wait_until(lambda: connections.send_to_client.called)
    envelope = MessageEnvelope.model_validate_json(connections.send_to_client.call_args[0][1])
    sent = CommandMessage.model_validate(envelope.payload)
    await controller.handle_response(ResponseMessage(command_id=sent.command_id, success=True, result=None))
    await task
    return sent.sim_speedup


async def test_a_simulated_device_no_run_paces_waits_at_the_servers_default_speed_up() -> None:
    """A device set to sim inside a LIVE run is still paced by its simulator, as it is for a command sent by hand."""
    assert await _sent_speedup(WorkflowRunMode.SIM, default_sim_speedup=2.0) == 2.0


async def test_a_live_device_is_never_paced() -> None:
    assert await _sent_speedup(WorkflowRunMode.LIVE, default_sim_speedup=2.0) is None


async def test_a_server_with_pacing_off_paces_no_simulated_device() -> None:
    assert await _sent_speedup(WorkflowRunMode.SIM, default_sim_speedup=None) is None
