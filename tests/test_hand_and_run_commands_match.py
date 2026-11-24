"""The same device command, sent by hand and by a run, leaves the same records.

Two identical benches. One is driven the way an operator drives it, through the operator runner. The other is
driven the way a run drives it: a move action for a plate move, and an action's device dispatcher for a liquid
handler command. After each step both benches must agree on every field of every op on each device, every position
holder, the liquid handler's own deck record and the labware events. Only who sent an op and when may differ: its
source, action, thread, timestamp, sequence and the bucket it is filed in.

A hand command reaches the SIM or LIVE world, so those are compared by hand and in a run. No hand command reaches the
CHECK_RUN world, so a check run is compared with the same run sent in SIM.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from cheshire_drivers.liquid_handler_models import GetDeckStateRequest
from pydantic import JsonValue

from orca.devices.devices import LiquidHandler
from orca.gateway.device_gateway import DeviceGateway
from orca.modes.device_modes import DeviceMode
from orca.gateway.operator_commands import OperatorDeviceCommands
from orca.resource_models.command_effects import Sender, SystemCommandPipeline, reach_params
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.resources import IModeAware
from orca.resource_models.transporter_base import TransporterBase
from orca.modes.run_modes import WorkflowRunMode, current_execution_id, mode_scope
from orca.runtime.server_events import ServerEvents
from orca.resource_models.sim_labware import SimTipSpot, SimWell
from orca.runtime.sinks import CollectorSink
from orca.state.ops_store import SYSTEM_ID, OpsHistorySearchQuery
from orca.state.records import TrackingSource
from orca.operations.error_mapping import error_label
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.workflow_models.actions import move_action
from orca.events.execution_context import DeviceCommandContext, MethodExecutionContext
from orca.resource_models.device_error import TipsNotPresentError
from orca.runtime.runtime_interface import ISystemRuntime
from orca.workflow_models.actions.device_call_dispatcher import CommandRecorder, DeviceCallDispatcher
from orca.workflow_models.actions.executable_location_action import ExecutableLocationAction
from orca.workflow_models.actions.move_action import MoveAction
from orca.workflow_models.device_handle import ActionRequest
from tests.test_adhoc_commands_are_recorded import (
    RESERVOIR_SITE,
    TIPS_SITE,
    WORKING_SITE,
    _Bench,
    _wire_to,
    running_arm_bench,
    running_lh_bench,
)
from tests.memory_activity import announced_labware_service, memory_runtime
from tests.test_deck_resident_reagent_pipetting import build_pipetting_bench
from tests.test_helpers import no_source_hold
from tests.recording_handler import RecordingHandler
from tests.device_modes import set_to
from tests.gateway_parts import gateway_with
from tests.real_device_path import real_device_path

pytestmark = pytest.mark.asyncio

RUN_ID = "run_1"
DECK_GRIPPER = "lh/gripper"

_OpenBench = Callable[[ServerEvents], AbstractAsyncContextManager[_Bench]]
# The last is args that do not read as a request, as a run's action can pass.
_CallArgs = tuple[list[SimTipSpot], ...] | tuple[list[SimWell], list[float]] | tuple[float, float] | tuple[str, str]
_Op = tuple[str, str]

# Who sent the op and when: these differ by sender and by clock, and nothing else may.
_SENDER_FIELDS = {"source", "action_id", "thread_id", "timestamp", "sequence"}


class _By(Enum):
    HAND = "hand"
    RUN = "run"


@dataclass(frozen=True)
class _Snapshot:
    """What a person reading the record sees after a step."""

    ledger: list[str | None]
    holders: list[list[str]]
    ops: dict[str, list[_Op]]
    deck: list[tuple[str, str | None]]
    contents: list[str]
    labware_events: list[tuple[str, str]]
    command_events: dict[str, list[tuple[str, str, str, str]]]

    def recorded(self, command: str) -> bool:
        return any(command in details or operation == command for ops in self.ops.values() for operation, details in ops)


class _Watched:
    """A bench, the server's event stream, and what it sent."""

    def __init__(self, bench: _Bench, events: ServerEvents, sink: CollectorSink, gateway: DeviceGateway) -> None:
        self.bench = bench
        self.events = events
        self.sink = sink
        self.gateway = gateway


@dataclass(frozen=True)
class _Side:
    """One bench, who sends its commands, and the world they land in."""

    watched: _Watched
    by: _By
    world: WorkflowRunMode

    @property
    def bench(self) -> _Bench:
        return self.watched.bench


_HAND_AND_RUN_WORLDS = pytest.mark.parametrize("world", [WorkflowRunMode.SIM, WorkflowRunMode.LIVE])
_WORLD_UP_OR_MARKED = pytest.mark.parametrize("world_up", [True, False], ids=["world_up", "first_command_rebuilds"])


@asynccontextmanager
async def _watched(open_bench: _OpenBench) -> AsyncIterator[_Watched]:
    sink = CollectorSink()
    events = ServerEvents()
    events.add_sink(sink)
    # Each bench has its own orca-client, as two servers would.
    async with real_device_path() as path, open_bench(events) as bench:
        # The device controller sends a command's STARTED and end events; the pipeline sends a refusal.
        path.events.add_sink(sink)
        # The server's command events come to this bench's stream too.
        bench.runtime.system.command_pipeline.report_to(events, bench.runtime, error_label)
        yield _Watched(bench, events, sink, path.gateway)


@contextmanager
def _as_a_run(side: _Side) -> Iterator[None]:
    """A run thread's context: its run mode and its execution, so its records are filed under the run."""
    token = current_execution_id.set(RUN_ID)
    try:
        with mode_scope(side.world):
            yield
    finally:
        current_execution_id.reset(token)


def _base(by: _By, world: WorkflowRunMode) -> WorkflowRunMode:
    """The mode a sender's command resolves against: an operator's the LIVE base, as `_adhoc` sends it; a run's its own."""
    return WorkflowRunMode.LIVE if by is _By.HAND else world


@asynccontextmanager
async def _two_sides(
    open_bench: _OpenBench, first: tuple[_By, WorkflowRunMode], second: tuple[_By, WorkflowRunMode],
    *, world_up: bool = True,
) -> AsyncIterator[tuple[_Side, _Side]]:
    async with AsyncExitStack() as stack:
        sides: list[_Side] = []
        for by, world in (first, second):
            # Built in its sender's base, so a bench lays out the world its commands land in.
            with mode_scope(_base(by, world)):
                watched = await stack.enter_async_context(_watched(open_bench))
            sides.append(_Side(watched, by, world))
        for side in sides:
            await _world_up(side, rebuild=world_up)
        yield sides[0], sides[1]


async def _world_up(side: _Side, *, rebuild: bool) -> None:
    """Every device in the side's world, set to live for LIVE, marked, and rebuilt unless `rebuild` is False.

    The real device path runs a LIVE command on the simulator, so LIVE needs no hardware. Whether a world is marked
    depends on what ran before, not on who sends the command, so every world is marked on both benches. Left
    marked, the first command of each sender rebuilds it, and those rebuilds are compared too.
    """
    runtime = side.bench.runtime
    devices = [r for r in runtime.system.resources if isinstance(r, IModeAware)]
    if side.world is WorkflowRunMode.LIVE:
        for device in devices:
            set_to(device, device.name, DeviceMode.LIVE)
    runtime.system.device_worlds.mark_everything()
    with mode_scope(_base(side.by, side.world)):
        for device in devices:
            assert await device.effective_mode() is side.world, f"{device.name} is not in the {side.world} world"
            if rebuild:
                await runtime.system.device_worlds.bring_up_to_date(device.name)


async def _state(watched: _Watched, *labware_names: str) -> _Snapshot:
    """What a person reading the record sees, with each bench's labware names and ids made alike."""
    bench = watched.bench
    system = bench.runtime.system
    alias: dict[str, str] = {}
    for i, name in enumerate(labware_names):
        alias[name] = f"labware_{i}"
        alias[system.get_labware(name).id] = f"labware_{i}_id"

    def plain(text: str) -> str:
        for name, same in alias.items():
            text = text.replace(name, same)
        return text

    # The bucket a record is filed in is the sender's: the run's execution or the operator's.
    ops: dict[str, list[_Op]] = {}
    for _bucket, record in await system.ops_history.store.search(OpsHistorySearchQuery()):
        for op in record.operations:
            fields = op.model_dump(exclude=_SENDER_FIELDS | {"operation", "device_name"})
            ops.setdefault(op.device_name, []).append((op.operation.value, plain(repr(fields))))
    deck: list[tuple[str, str | None]] = []
    for lh in (device for device in system.devices if isinstance(device, LiquidHandler)):
        state = await lh.driver.get_deck_state(GetDeckStateRequest())
        deck += [(plain(item.name), item.site) for item in state.labware]
    contents: list[str] = []
    for name in labware_names:
        snapshot = await bench.runtime.labware.resolve_contents(system.get_labware(name).id)
        contents.append(repr((
            snapshot.provenance, snapshot.source, snapshot.tip_positions_present, snapshot.tip_count, snapshot.volumes,
        )))
    # Each device's own command events in order; across devices a run's move and an operator's differ by design.
    commands: dict[str, list[tuple[str, str, str, str]]] = {}
    for event in watched.sink.events:
        if event.event_name.startswith("COMMAND.") and isinstance(event.context, DeviceCommandContext):
            commands.setdefault(event.context.device_name, []).append((
                event.status or "", event.context.command, plain(repr(event.context.params)),
                (event.context.error or "").split(":", 1)[0],
            ))
    moves = [
        (event.event_name.split(".", 2)[0], plain(repr(getattr(event.context, "position_id", None))))
        for event in watched.sink.events if event.event_name.startswith("LABWARE.")
    ]
    return _Snapshot(
        ledger=[bench.site_of(name) for name in labware_names],
        holders=[bench.holders_of(name) for name in labware_names],
        ops=ops,
        deck=deck,
        contents=contents,
        labware_events=moves,
        command_events=commands,
    )


async def _assert_sent_as_its_sender_sends(side: _Side) -> None:
    """Every command went to the side's world, and a run's commands are filed under the run, an operator's outside it."""
    # A command event is stamped with its mode when it is sent; a refused one never was.
    modes = {
        event.context.mode for event in side.watched.sink.events
        if event.event_name.startswith("COMMAND.") and isinstance(event.context, DeviceCommandContext)
        and event.context.mode is not None
    }
    assert modes <= {side.world.value}, f"commands went to {modes}, not the {side.world} world"
    hits = await side.bench.runtime.system.ops_history.store.search(OpsHistorySearchQuery())
    sent = {
        bucket for bucket, record in hits for op in record.operations
        if getattr(op.details, "command", None) in _SENT or op.operation.value in _SENT
    }
    assert sent, "the side sent none of the compared commands"
    assert (sent == {RUN_ID}) is (side.by is _By.RUN), f"{side.by.value} commands were filed under {sent}"


# The commands the scenarios send; the engine's own upkeep around them is filed by whoever caused it.
_SENT = frozenset({"pick_at_coords", "place_at_coords", "pick_up_tips", "aspirate", "dispense"})


async def _commands_in_order(bench: _Bench) -> list[tuple[str, str]]:
    """Every device and command the record holds, in the order they were saved, across devices."""
    hits = await bench.runtime.system.ops_history.store.search(OpsHistorySearchQuery())
    return [
        (op.device_name, getattr(op.details, "command", op.operation.value))
        for _bucket, record in hits for op in record.operations
    ]


def _operator(side: _Side) -> OperatorDeviceCommands:
    """The operator surface a REST or MCP command enters, on this bench's own gateway and orca-client."""
    return OperatorDeviceCommands(side.watched.gateway, lambda: side.bench.runtime)


def _operator_on_a_stand_in_wire(side: _Side) -> OperatorDeviceCommands:
    """An operator whose gateway answers for any device id and runs the command on the driver directly."""
    tracker_snapshot = MagicMock()
    tracker_snapshot.interfaces = ["ILiquidHandler"]
    tracker = MagicMock()
    tracker.get_device = AsyncMock(return_value=tracker_snapshot)
    controller = MagicMock()
    controller.execute_command = AsyncMock(side_effect=_wire_to(side.bench))
    controller.command_events = side.watched.events
    return OperatorDeviceCommands(gateway_with(tracker=tracker, controller=controller), lambda: side.bench.runtime)


async def _by_hand(side: _Side, device_id: str, command: str, params: dict[str, JsonValue]) -> None:
    # The gateway registers no deck gripper, so a hand command to one cannot go through the real gateway.
    operator = _operator_on_a_stand_in_wire(side) if device_id == DECK_GRIPPER else _operator(side)
    with mode_scope(WorkflowRunMode.LIVE):
        await operator.run_named(device_id, command, params)


def _status_manager() -> Mock:
    return Mock(set_status=Mock())


def _thread_context() -> Mock:
    return Mock(
        execution_id=RUN_ID, workflow_name="hand_and_run", thread_id="thread_1",
        thread_name="thread_1", template_name="journey", group_id=None,
    )


async def _run_move(
    bench: _Bench, mover_name: str, source: str, target: str, after_pick: Callable[[], Awaitable[None]],
) -> None:
    """A run's move, as a thread runs it, with a look at the record between its pick and its place."""
    system = bench.runtime.system
    mover = next(m for m in system.movers if m.name == mover_name)
    assert isinstance(mover, TransporterBase)
    labware = system.get_labware(bench.labware_name)
    source_site = system.system_map.resolve_placement_location(source)
    target_site = system.system_map.resolve_placement_location(target)
    move = MoveAction(labware, source_site, target_site, mover)
    reservation = LocationReservation(requested_location=target_site, labware=labware)
    reservation.set_location(target_site)
    move.set_reservation(reservation)
    real_place = move_action.place_with_the_arm

    async def place_after_looking(
        pipeline: SystemCommandPipeline, mover: TransporterBase, site: Location, labware: LabwareInstance,
        picked_from: Location | None, sender: Sender,
    ) -> None:
        await after_pick()
        await real_place(pipeline, mover, site, labware, picked_from, sender)

    with patch.object(move_action, "place_with_the_arm", place_after_looking):
        await move.executable(
            _status_manager(), _thread_context(), system.labware_location_service,
            system.command_pipeline, no_source_hold(),
        ).execute()


async def _move(side: _Side, mover: str, source: str, target: str) -> _Snapshot:
    """The side's plate move from source to target; returns what the record showed between the pick and the place."""
    after_pick: list[_Snapshot] = []

    async def look() -> None:
        after_pick.append(await _state(side.watched, side.bench.labware_name))

    if side.by is _By.HAND:
        await _by_hand(side, mover, "pick_at_coords", reach_params(source))
        await look()
        await _by_hand(side, mover, "place_at_coords", reach_params(target))
    else:
        with _as_a_run(side):
            await _run_move(side.bench, mover, source, target, look)
    return after_pick[0]


def _without_device(state: _Snapshot, position: str) -> _Snapshot:
    device = position.split("/", 1)[0]
    return replace(
        state,
        ops={name: ops for name, ops in state.ops.items() if name != device},
        command_events={name: events for name, events in state.command_events.items() if name != device},
    )


def _site_name(bench: _Bench, position: str) -> str:
    return bench.runtime.system.system_map.resolve_placement_location(position).position_id


async def _compare_moves(first: _Side, second: _Side, mover: str, source: str, target: str) -> None:
    name = first.bench.labware_name
    assert await _state(first.watched, name) == await _state(second.watched, second.bench.labware_name)

    first_after_pick = await _move(first, mover, source, target)
    second_after_pick = await _move(second, mover, source, target)

    assert first_after_pick.recorded("pick_at_coords")
    if first.by is second.by:
        assert first_after_pick == second_after_pick
    else:
        # A run's move readies the target before its pick; an operator readies it with the place.
        assert _without_device(first_after_pick, target) == _without_device(second_after_pick, target)
    after_place = await _state(first.watched, name)
    assert after_place.recorded("place_at_coords")
    assert after_place == await _state(second.watched, second.bench.labware_name)
    assert first.bench.site_of(name) == _site_name(first.bench, target)
    for side in (first, second):
        await _assert_sent_as_its_sender_sends(side)


@_HAND_AND_RUN_WORLDS
@_WORLD_UP_OR_MARKED
@pytest.mark.timeout(60)
async def test_a_transporter_move_leaves_the_same_records_on_each_device_and_only_a_run_opens_the_target_first(
    world: WorkflowRunMode, world_up: bool,
) -> None:
    """A run's move is one unit and readies both ends before the pick. An operator's pick cannot know where the plate
    goes, so the target opens with the place."""
    async with _two_sides(running_arm_bench, (_By.HAND, world), (_By.RUN, world), world_up=world_up) as (by_hand, by_run):
        await _compare_moves(by_hand, by_run, "arm", "pad", "stacker")

        hand = await _commands_in_order(by_hand.bench)
        run = await _commands_in_order(by_run.bench)
        opened, picked, placed = ("stacker", "open"), ("arm", "pick_at_coords"), ("arm", "place_at_coords")
        assert run.index(opened) < run.index(picked), "a run opens the target before its pick"
        assert hand.index(picked) < hand.index(opened) < hand.index(placed), "an operator opens it with the place"


@_HAND_AND_RUN_WORLDS
@_WORLD_UP_OR_MARKED
@pytest.mark.timeout(60)
async def test_a_deck_gripper_pick_then_place_leave_the_same_records_by_hand_and_in_a_run(
    world: WorkflowRunMode, world_up: bool,
) -> None:
    """The gripper's pick moves nothing and its place sends move_plate: the driver keeps the plate throughout."""
    async with _two_sides(running_lh_bench, (_By.HAND, world), (_By.RUN, world), world_up=world_up) as (by_hand, by_run):
        await _compare_moves(by_hand, by_run, DECK_GRIPPER, RESERVOIR_SITE, WORKING_SITE)


async def _register(side: _Side, template: str, site: str) -> str:
    bench = side.bench
    with mode_scope(_base(side.by, side.world)):
        snapshot = await bench.runtime.labware.register(template, location=site, confirm=True)
    return next(lw.name for lw in await bench.runtime.labware.list_all() if lw.id == snapshot.id)


async def _run_command(runtime: ISystemRuntime, device_name: str, command: str, args: _CallArgs) -> None:
    """A device command as a run's action sends it: the action's own recorder, saving through the tracking context,
    and the labware the action names, as `LocationAction` builds them."""
    system = runtime.system
    device = system.get_device(device_name)
    action = Mock(id="action_1", command=command, device=device, declares=None)
    action.build_template_to_instance_map.return_value = {}
    executable = ExecutableLocationAction(
        _status_manager(), action,
        MethodExecutionContext(
            execution_id=RUN_ID, workflow_name="hand_and_run", method_id="m1", method_name="method",
            thread_id="thread_1", thread_name="thread_1", participating_thread_ids=("thread_1",),
        ),
        tracking_context=system.tracking_context,
    )
    executable._wire_recorder()
    recorder: CommandRecorder = action.set_recorder.call_args[0][0]
    standing = device.engine_commands.pipeline.labware_at(device.name)
    dispatcher = DeviceCallDispatcher(
        device=device, recorder=recorder, pipeline=device.engine_commands.pipeline,
        action_id="action_1", execution_id=RUN_ID,
    )
    await dispatcher.dispatch(
        ActionRequest(device_name=device.name, command=command, args=args, kwargs={}),
        [i.name for i in standing], [i.id for i in standing], list(system.labwares),
    )


@asynccontextmanager
async def _pipetting_bench(events: ServerEvents) -> AsyncIterator[_Bench]:
    """A liquid handler with a tip rack and a reservoir on its deck. The bench's labware is the rack."""
    recorder = RecordingHandler()
    build, lh = await build_pipetting_bench(recorder, labware_service=announced_labware_service(events))
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    bench = _Bench(runtime, lh.name, "", "")
    try:
        bench.labware_name = await _register_on(bench, "tips", TIPS_SITE)
        bench.labware_id = runtime.system.get_labware(bench.labware_name).id
        await runtime.labware.set_tip_state(bench.labware_id, ["A1", "B1"], reason="bench setup", confirm=True)
        yield bench
    finally:
        await runtime.shutdown()


async def _register_on(bench: _Bench, template: str, site: str) -> str:
    snapshot = await bench.runtime.labware.register(template, location=site, confirm=True)
    return next(lw.name for lw in await bench.runtime.labware.list_all() if lw.id == snapshot.id)


async def _pick_up_tip(side: _Side, rack: str) -> None:
    if side.by is _By.HAND:
        await _by_hand(side, side.bench.lh_name, "pick_up_tips", {"picks": [{"tip_rack": rack, "positions": ["A1"]}]})
        return
    with _as_a_run(side):
        await _run_command(
            side.bench.runtime, side.bench.lh_name, "pick_up_tips", ([SimTipSpot(identifier="A1", parent_name=rack)],),
        )


async def _move_liquid(side: _Side, command: str, reservoir: str) -> None:
    if side.by is _By.HAND:
        key = "aspirations" if command == "aspirate" else "dispenses"
        await _by_hand(
            side, side.bench.lh_name, command, {key: [{"labware": reservoir, "positions": ["A1"], "volumes": [20.0]}]},
        )
        return
    with _as_a_run(side):
        await _run_command(
            side.bench.runtime, side.bench.lh_name, command, ([SimWell(parent_name=reservoir, identifier="A1")], [20.0]),
        )


async def _compare_pipetting(first: _Side, second: _Side) -> None:
    labware = []
    for side in (first, second):
        labware.append((side.bench.labware_name, await _register(side, "reservoir", RESERVOIR_SITE)))
    a, b = labware
    assert await _state(first.watched, *a) == await _state(second.watched, *b)

    for side, (rack, _) in zip((first, second), labware):
        await _pick_up_tip(side, rack)
    after = await _state(first.watched, *a)
    assert after.recorded("pick_up_tips") and "['B1']" in after.contents[0], "the tip at A1 is off the rack"
    assert after == await _state(second.watched, *b)

    for side, (rack, _) in zip((first, second), labware):
        with pytest.raises(TipsNotPresentError):
            await _pick_up_tip(side, rack)
    after = await _state(first.watched, *a)
    assert after == await _state(second.watched, *b)
    for side in (first, second):
        handler = (await _state(side.watched, *(a if side is first else b))).command_events[side.bench.lh_name]
        picks = [event for event in handler if event[1] == "pick_up_tips" and event[0] != "STARTED"]
        assert [status for status, _, _, _ in picks] == ["COMPLETED", "FAILED"], "a refusal ends FAILED"
        assert picks[1][2] == picks[0][2], f"{side.by.value}: a refused pick names the params a sent one carried"

    for command in ("aspirate", "dispense"):
        for side, (_, reservoir) in zip((first, second), labware):
            await _move_liquid(side, command, reservoir)
        after = await _state(first.watched, *a)
        assert after.recorded(command)
        assert after == await _state(second.watched, *b)
    for side in (first, second):
        await _assert_sent_as_its_sender_sends(side)


@_HAND_AND_RUN_WORLDS
@_WORLD_UP_OR_MARKED
@pytest.mark.timeout(60)
async def test_a_tip_pick_up_an_aspirate_and_a_dispense_leave_the_same_records_by_hand_and_in_a_run(
    world: WorkflowRunMode, world_up: bool,
) -> None:
    async with _two_sides(_pipetting_bench, (_By.HAND, world), (_By.RUN, world), world_up=world_up) as (by_hand, by_run):
        await _compare_pipetting(by_hand, by_run)


_CHECK_RUN_AND_SIM = ((_By.RUN, WorkflowRunMode.CHECK_RUN), (_By.RUN, WorkflowRunMode.SIM))


@pytest.mark.timeout(60)
async def test_a_transporter_move_in_a_check_run_leaves_the_records_it_leaves_in_a_sim_run() -> None:
    async with _two_sides(running_arm_bench, *_CHECK_RUN_AND_SIM) as (check_run, sim_run):
        await _compare_moves(check_run, sim_run, "arm", "pad", "stacker")


@pytest.mark.timeout(60)
async def test_a_deck_gripper_move_in_a_check_run_leaves_the_records_it_leaves_in_a_sim_run() -> None:
    async with _two_sides(running_lh_bench, *_CHECK_RUN_AND_SIM) as (check_run, sim_run):
        await _compare_moves(check_run, sim_run, "lh/gripper", RESERVOIR_SITE, WORKING_SITE)


@pytest.mark.timeout(60)
async def test_pipetting_in_a_check_run_leaves_the_records_it_leaves_in_a_sim_run() -> None:
    async with _two_sides(_pipetting_bench, *_CHECK_RUN_AND_SIM) as (check_run, sim_run):
        await _compare_pipetting(check_run, sim_run)


@pytest.mark.timeout(60)
async def test_a_runs_arm_move_is_filed_under_the_run_but_outside_its_thread_and_action() -> None:
    """Today a run's pick and place are saved by name: the run's bucket, but no thread or action of the run's.

    A run's liquid handler commands carry the run's thread and action. When a run's move does too, flip this test.
    """
    async with _two_sides(running_arm_bench, (_By.RUN, WorkflowRunMode.SIM), (_By.RUN, WorkflowRunMode.SIM)) as (run, _):
        await _move(run, "arm", "pad", "stacker")

        hits = await run.bench.runtime.system.ops_history.store.search(OpsHistorySearchQuery())
        reaches = [
            (bucket, op.source, op.thread_id, op.action_id.split(":", 1)[0])
            for bucket, record in hits for op in record.operations
            if getattr(op.details, "command", None) in ("pick_at_coords", "place_at_coords")
        ]
        assert reaches == [(RUN_ID, TrackingSource.OBSERVED, SYSTEM_ID, "__observed__")] * 2


@pytest.mark.timeout(60)
async def test_a_run_command_whose_args_cannot_be_read_ends_failed_as_a_refusal_does() -> None:
    async with _two_sides(_pipetting_bench, (_By.RUN, WorkflowRunMode.SIM), (_By.RUN, WorkflowRunMode.SIM)) as (run, _):
        with pytest.raises(TypeError, match="takes at most 1 arguments"), _as_a_run(run):
            await _run_command(run.bench.runtime, run.bench.lh_name, "pick_up_tips", ("not a tip spot", "nor this"))

        handler = (await _state(run.watched, run.bench.labware_name)).command_events[run.bench.lh_name]
        assert [(status, command) for status, command, _, _ in handler][-1:] == [("FAILED", "pick_up_tips")]
