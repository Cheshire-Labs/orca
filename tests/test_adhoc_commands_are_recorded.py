"""An operator's own device command has to leave the same trail a workflow's does.

Two dispatch paths reach a device: a workflow action, which runs the operation
interpreter and writes the ledger, and an ad-hoc command, which goes straight to
the driver. Only the first recorded anything, so every manual move, pick-up and
dispense an operator made during a recovery was invisible to the ledger -- and
the ledger is both what every operator surface reads AND what the next deck
reconcile pushes back down over the driver.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from pydantic import JsonValue
from orca.resource_models.transporter import Transporter
from cheshire_drivers.transporter_models import PickAtCoordsRequest
import pytest_asyncio
from cheshire_drivers.liquid_handler_models import (
    DropTipsRequest,
    GetDeckStateRequest,
    PickUpTipsRequest,
    TipPick,
)

from orca.gateway import operator_commands, operator_recorder
from orca.gateway.controller.command_pipeline import DeviceCommandPipeline, PartialChannelFailureError
from orca.resource_models.command_effects import CommandStatement, device_move
from tests.gateway._operator_command import run_operator_command
from orca.gateway.operator_commands import OperatorDeviceCommands
from orca.operations.operator_device import BatchCommand, NamedCommands, run_batch
from orca.operations.operator_device_models import BatchCommandResultDTO
from orca.gateway.controller.exceptions import CommandNotSavedError, InvalidCommandError
from orca.gateway.registry.snapshot import DeviceSnapshot
from orca.resource_models.device_error import MoverAlreadyHoldingError, TipsNotPresentError
from orca.resource_models.labware_location_service import PlacementState
from orca.gateway.operator_recorder import record_operator_command
from orca.runtime.incident_store import (
    IncidentCategory,
)
from orca.events.emitter import IEventEmitter
from orca.runtime.incident_service import IncidentService
from orca.runtime.server_events import ServerEvents
from orca.runtime.sinks import CollectorSink
from orca.events.execution_context import LabwareRecordContext
from orca.modes.run_modes import WorkflowRunMode, current_run_mode
from orca.runtime.system_runtime import SystemRuntime
from orca.state.command_operations import ChannelFailureNotReadError, RecordContext, operations_for_request
from orca.state.ops_store import OpsHistorySearchQuery
from orca.state.provenance import Provenance
from orca.state.records import (
    AspirateDetails,
    DeviceOperation,
    GenericOperationDetails,
    ObservationGapCause,
    OperationDetails,
    OperationRecord,
    TipDrop96Details,
    TipDropDetails,
    TipPickUpDetails,
    TrackingSource,
)
from orca.events.execution_context import MethodExecutionContext
from orca.workflow_models.actions.device_call_dispatcher import CommandRecorder
from orca.workflow_models.actions.executable_location_action import ExecutableLocationAction
from orca.workflow_models.device_handle import ActionRequest
from tests.gateway_parts import gateway_with
from orca.resource_models.labware_service_interface import ILabwareService
from tests.memory_activity import announced_labware_service, memory_labware_service, memory_runtime
from tests.test_deck_resident_reagent_pipetting import build_pipetting_bench
from tests.test_labware_state_reconciliation import RESERVOIR_SITE, _build
from tests.recording_handler import RecordingHandler

WORKING_SITE = "lh/carrier-7-0"
TIPS_SITE = "lh/carrier-15-0"


class _Bench:
    """A runtime with one liquid handler whose driver is reachable both ways."""

    def __init__(
        self, runtime: SystemRuntime, lh_name: str,
        labware_name: str, labware_id: str,
    ) -> None:
        self.runtime = runtime
        self.lh_name = lh_name
        self.labware_name = labware_name
        self.labware_id = labware_id

    def ledger_site(self) -> str | None:
        return self.site_of(self.labware_name)

    def site_of(self, labware_name: str) -> str | None:
        instance = self.runtime.system.get_labware(labware_name)
        service = self.runtime.system.labware_location_service
        try:
            return service.get(instance).position_id
        except KeyError:
            return None

    def holders_of(self, labware_name: str) -> list[str]:
        """Every position holder whose slot currently reads this labware."""
        system = self.runtime.system
        instance = system.get_labware(labware_name)
        slots = list(system.system_map.locations)
        slots.extend(mover.gripper_location for mover in system.movers)
        return sorted(s.position_id for s in slots if s.labware is instance)


@pytest_asyncio.fixture
async def bench() -> AsyncIterator[_Bench]:
    async with running_lh_bench() as built:
        yield built


def _labware_record(labware_events: IEventEmitter | None) -> ILabwareService:
    return memory_labware_service() if labware_events is None else announced_labware_service(labware_events)


@asynccontextmanager
async def running_lh_bench(labware_events: IEventEmitter | None = None) -> AsyncIterator[_Bench]:
    """A liquid handler with a reservoir registered on its deck."""
    recorder = RecordingHandler()
    build, lh = await _build(recorder, wf_name="adhoc_recording", labware_service=_labware_record(labware_events))
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    snapshot = await runtime.labware.register(
        "reservoir", location=RESERVOIR_SITE, confirm=True,
    )
    name = next(
        lw.name for lw in await runtime.labware.list_all() if lw.id == snapshot.id
    )
    try:
        yield _Bench(runtime, lh.name, name, snapshot.id)
    finally:
        await runtime.shutdown()


async def _nothing_sent() -> None:
    return None


async def _nothing_recorded(_answer: None) -> None:
    return None


def _wire_to(bench: _Bench):
    """Stand in for the WebSocket only: run the command on the real driver."""

    async def execute_command(*, device_id: str, command: str,
                              params: dict[str, Any], **_: Any) -> Any:
        from cheshire_drivers.command_table import COMMANDS

        found = COMMANDS.find({"ILiquidHandler"}, command)
        if found is None:
            # An arm command under external control: the driver validates and
            # mutates nothing by contract, so there is no driver state to drive.
            return None
        device = bench.runtime.system.get_device(bench.lh_name)
        result = await getattr(device.driver, command)(
            **found.driver_kwargs(params)
        )
        return result.model_dump(mode="json") if result is not None else None

    return execute_command


async def _adhoc(bench: _Bench, command: str, params: dict[str, Any]) -> None:
    """Dispatch as an operator's own command, in the world an operator is in."""
    tracker_snapshot = MagicMock()
    tracker_snapshot.interfaces = ["ILiquidHandler"]
    token = current_run_mode.set(WorkflowRunMode.LIVE)
    try:
        tracker = MagicMock()
        controller = MagicMock()
        tracker.get_device = AsyncMock(return_value=tracker_snapshot)
        controller.execute_command = AsyncMock(side_effect=_wire_to(bench))
        await run_operator_command(
            gateway_with(tracker=tracker, controller=controller),
            bench.runtime, bench.lh_name, command, params,
        )
    finally:
        current_run_mode.reset(token)


@pytest.mark.timeout(30)
async def test_an_adhoc_move_puts_the_labware_where_it_actually_went(
    bench: _Bench,
) -> None:
    """The command an operator uses to move a plate is the one that lost it.

    A deck addresses labware by site, so the driver re-parents it and knows.
    The ledger is not on that path, and the ledger is what every read answers
    from and what the next reconcile writes back down.
    """
    assert bench.ledger_site() == RESERVOIR_SITE

    await _adhoc(bench, "move_plate", {
        "plate": bench.labware_name,
        "to_position": WORKING_SITE.split("/", 1)[1],
        "from_position": RESERVOIR_SITE.split("/", 1)[1],
    })

    assert bench.ledger_site() == WORKING_SITE, (
        "the operator moved it and the driver agrees; a ledger still naming the "
        "source is the answer every surface gives and the one the next deck "
        "reconcile pushes back onto the driver"
    )


@pytest.mark.out_of_run
@pytest.mark.timeout(30)
async def test_an_adhoc_move_on_a_liquid_handler_the_topology_does_not_declare_is_sent(bench: _Bench) -> None:
    """No site of an undeclared device is in the record, so the move states no labware motion and goes to the device."""
    undeclared = "lh_the_topology_does_not_declare"
    tracker = MagicMock()
    tracker.get_device = AsyncMock(return_value=DeviceSnapshot(
        type="liquid_handler", name=undeclared, interfaces=["ILiquidHandler"], capabilities=[],
        provides_state=True, methods={}, site="test", lab="test", workcell=None, status="ready",
        last_seen=datetime.now(timezone.utc),
    ))
    controller = MagicMock()
    controller.execute_command = AsyncMock(return_value=None)
    token = current_run_mode.set(WorkflowRunMode.LIVE)
    try:
        await run_operator_command(
            gateway_with(tracker=tracker, controller=controller),
            bench.runtime, undeclared, "move_plate", {
                "plate": bench.labware_name,
                "to_position": WORKING_SITE.split("/", 1)[1],
                "from_position": RESERVOIR_SITE.split("/", 1)[1],
            },
        )
    finally:
        current_run_mode.reset(token)

    controller.execute_command.assert_awaited_once()
    assert bench.ledger_site() == RESERVOIR_SITE


@pytest.mark.out_of_run
@pytest.mark.timeout(30)
async def test_an_adhoc_move_reaches_the_driver_deck_too(bench: _Bench) -> None:
    """A move within one deck leaves the plate at one site, and says so.

    Both slots hold the plate for as long as it takes to clear the source, and
    the deck reconcile refuses a plate at two sites of one device outright. So
    a projection driven from the middle of the pair does not merely arrive
    early, it never arrives at all, and the driver keeps the old site.
    """
    await _adhoc(bench, "move_plate", {
        "plate": bench.labware_name,
        "to_position": WORKING_SITE.split("/", 1)[1],
        "from_position": RESERVOIR_SITE.split("/", 1)[1],
    })

    assert bench.holders_of(bench.labware_name) == [WORKING_SITE]
    device = bench.runtime.system.get_device(bench.lh_name)
    state = await device.driver.get_deck_state(GetDeckStateRequest())
    sites = {item.name: item.site for item in state.labware}
    assert sites.get(bench.labware_name) == WORKING_SITE.split("/", 1)[1], (
        "the ledger and the driver both have to end up naming the new site; "
        f"the driver reports {sites!r}"
    )


@pytest_asyncio.fixture
async def tip_bench() -> AsyncIterator[_Bench]:
    recorder = RecordingHandler()
    build, lh = await build_pipetting_bench(recorder)
    runtime = await memory_runtime(
        build.system, event_bus=build.event_bus
    )
    await runtime.start()
    snapshot = await runtime.labware.register("tips", location=TIPS_SITE, confirm=True)
    name = next(
        lw.name for lw in await runtime.labware.list_all() if lw.id == snapshot.id
    )
    bench = _Bench(runtime, lh.name, name, snapshot.id)
    try:
        yield bench
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(30)
async def test_an_adhoc_tip_pickup_leaves_the_rack_layout_true(
    tip_bench: _Bench,
) -> None:
    """A manual pick-up empties a rack spot, and the ledger folds the rack.

    The driver reports the whole rack back on every call; only the workflow
    path read that report. So an operator who picks a tip by hand leaves the
    ledger claiming a tip that is now on a nozzle, and the next deck reconcile
    seeds the driver's rack from that claim.
    """
    runtime = tip_bench.runtime
    await runtime.labware.set_tip_state(
        tip_bench.labware_id, ["A1", "B1"], reason="bench setup", confirm=True,
    )
    before = await runtime.labware.get_tip_state(tip_bench.labware_id)
    assert before.positions_present == ["A1", "B1"]

    await _adhoc(tip_bench, "pick_up_tips", {
        "picks": [{"tip_rack": tip_bench.labware_name, "positions": ["A1"]}],
    })

    after = await runtime.labware.get_tip_state(tip_bench.labware_id)
    assert after.positions_present == ["B1"], (
        "the A1 tip is on a nozzle; a ledger that still counts it hands the next "
        "pick a spot that is empty and re-seeds the driver with a tip that is gone"
    )


@pytest.mark.timeout(30)
async def test_a_pick_the_record_says_is_impossible_is_refused_before_it_is_sent(
    tip_bench: _Bench,
) -> None:
    """An operator's tip pick-up passes the check a run's does: holes the record calls empty, on a rack someone has
    looked at, refuse the pick before the driver is told. The record is left as the operator last stated it."""
    runtime = tip_bench.runtime
    await runtime.labware.set_tip_state(
        tip_bench.labware_id, ["A1"], reason="bench setup", confirm=True,
    )
    settled = await runtime.labware.resolve_contents(tip_bench.labware_id)
    assert settled.provenance is Provenance.KNOWN
    with pytest.raises(TipsNotPresentError, match="A2"):
        await _adhoc(tip_bench, "pick_up_tips", {
            "picks": [{"tip_rack": tip_bench.labware_name, "positions": ["A2"]}],
        })

    assert [
        op for op in await _all_ops(tip_bench) if op.operation is DeviceOperation.PICK_UP_TIPS
    ] == [], "a refused command leaves no record"
    after = await runtime.labware.resolve_contents(tip_bench.labware_id)
    assert after.provenance is Provenance.KNOWN


@pytest.mark.timeout(30)
async def test_a_pick_the_record_backs_says_nothing(tip_bench: _Bench) -> None:
    """The ordinary case stays silent. An incident per manual pick would be an
    incident list nobody reads."""
    runtime = tip_bench.runtime
    await runtime.labware.set_tip_state(
        tip_bench.labware_id, ["A1"], reason="bench setup", confirm=True,
    )

    await _adhoc(tip_bench, "pick_up_tips", {
        "picks": [{"tip_rack": tip_bench.labware_name, "positions": ["A1"]}],
    })

    assert await runtime.incidents.list(
        category=IncidentCategory.LEDGER_CONTRADICTED,
    ) == []
    after = await runtime.labware.resolve_contents(tip_bench.labware_id)
    assert after.provenance is Provenance.KNOWN


@pytest.mark.timeout(30)
async def test_a_stale_rack_is_not_a_contradiction(tip_bench: _Bench) -> None:
    """After a restart every rack is stale, which is the recovery bench.

    A stale fold describes then, not now, so a pick finding a tip where it
    reads empty is what a stale reading looks like when nobody has confirmed
    it yet. Filing an incident there fires on the operator who is already
    doing the right thing, and adds a second observation gap to a labware
    that is already asking to be looked at.
    """
    runtime = tip_bench.runtime
    await runtime.labware.set_tip_state(
        tip_bench.labware_id, ["A1"], reason="bench setup", confirm=True,
    )
    instance = next(
        l for l in runtime._system.labwares if l.id == tip_bench.labware_id
    )
    await instance.note_observation_gap(ObservationGapCause.RUNTIME_RESTART)
    assert await instance.contents_provenance() is Provenance.STALE

    await _adhoc(tip_bench, "pick_up_tips", {
        "picks": [{"tip_rack": tip_bench.labware_name, "positions": ["A2"]}],
    })

    assert await runtime.incidents.list(
        category=IncidentCategory.LEDGER_CONTRADICTED,
    ) == []


@pytest.mark.timeout(30)
async def test_a_rack_nobody_described_is_not_a_contradiction(
    tip_bench: _Bench,
) -> None:
    """Nothing said is not the same as said-empty. Treating an undescribed rack
    as contradicted would file an incident for every first manual pick."""
    await _adhoc(tip_bench, "pick_up_tips", {
        "picks": [{"tip_rack": tip_bench.labware_name, "positions": ["A2"]}],
    })

    assert await tip_bench.runtime.incidents.list(
        category=IncidentCategory.LEDGER_CONTRADICTED,
    ) == []


@asynccontextmanager
async def running_arm_bench(
    labware_events: IEventEmitter | None = None, incident_service: IncidentService | None = None,
) -> AsyncIterator[_Bench]:
    """An arm, a pad, a storage device and a liquid handler, with a plate on the pad."""
    recorder = RecordingHandler()
    build, lh = await _build(recorder, wf_name="adhoc_arm", labware_service=_labware_record(labware_events))
    runtime = await memory_runtime(build.system, event_bus=build.event_bus, incident_service=incident_service)
    await runtime.start()
    snapshot = await runtime.labware.register(
        "sample_plate", location="pad", confirm=True,
    )
    name = next(
        lw.name for lw in await runtime.labware.list_all() if lw.id == snapshot.id
    )
    bench = _Bench(runtime, "arm", name, snapshot.id)
    try:
        yield bench
    finally:
        await runtime.shutdown()


@pytest_asyncio.fixture
async def arm_bench() -> AsyncIterator[_Bench]:
    async with running_arm_bench() as bench:
        yield bench


async def _commands_named(bench: _Bench, command: str) -> list[str]:
    return [
        op.device_name for op in await _all_ops(bench)
        if op.operation is DeviceOperation.COMMAND
        and isinstance(op.details, GenericOperationDetails) and op.details.command == command
    ]


@pytest.mark.timeout(30)
async def test_putting_labware_on_a_deck_saves_the_push_to_the_driver(tip_bench: _Bench) -> None:
    """The engine tells the driver its deck changed; that is a command like any other."""
    assert await _commands_named(tip_bench, "reconcile_deck_occupancy") == [tip_bench.lh_name]


@pytest.mark.timeout(30)
async def test_a_hand_move_shows_between_a_place_and_a_pick_somewhere_else(arm_bench: _Bench) -> None:
    """A plate placed at one site and later picked from another has the move between them on its history."""
    await arm_bench.runtime.labware.edit_location(
        arm_bench.labware_id, "stacker/slot", reason="moved by hand", confirm=True,
    )
    await _adhoc(arm_bench, "pick_at_coords", _teachpoint("stacker"))

    history = await arm_bench.runtime.labware.get_history(arm_bench.labware_id)
    assert [event.position_id for event in history][-3:] == ["pad", "stacker/slot", "arm/gripper"]


def _teachpoint(position_id: str) -> dict[str, Any]:
    return {"teachpoint": {"position_id": position_id}}


@pytest.mark.timeout(30)
async def test_an_adhoc_arm_pick_puts_the_plate_in_the_jaws(
    arm_bench: _Bench,
) -> None:
    """The arm's own pick names a teachpoint, never a labware.

    So the plate it took is whatever the ledger had there, and the ledger has a
    real position for the jaws. Leaving the plate on the pad says the arm is
    empty and the pad is full, and both are wrong at once.
    """
    assert arm_bench.ledger_site() == "pad"

    await _adhoc(arm_bench, "pick_at_coords", _teachpoint("pad"))

    assert arm_bench.ledger_site() == "arm/gripper"


@pytest.mark.timeout(30)
async def test_an_adhoc_arm_place_puts_the_plate_down_again(
    arm_bench: _Bench,
) -> None:
    """'stacker' is a device, so the plate lands on its site, not its name.

    The device name is the reservation mutex and holds no labware; a plate
    recorded there is at a position nothing can route to or reserve past.
    """
    await _adhoc(arm_bench, "pick_at_coords", _teachpoint("pad"))

    await _adhoc(arm_bench, "place_at_coords", _teachpoint("stacker"))

    assert arm_bench.ledger_site() == "stacker/slot"


def _arm_requests(bench: _Bench) -> list[tuple[str, str]]:
    """Each pick and place the arm's own driver is sent, with the labware type it carries."""
    sent: list[tuple[str, str]] = []
    arm = next(m for m in bench.runtime.system.movers if m.name == bench.lh_name)
    assert isinstance(arm, Transporter)
    for command in ("pick_at_coords", "place_at_coords"):
        real = getattr(arm.driver, command)

        async def recorded(request: PickAtCoordsRequest, real=real, command=command) -> None:
            sent.append((command, request.labware_type))
            await real(request)

        setattr(arm.driver, command, recorded)
    return sent


@pytest.mark.timeout(30)
async def test_a_pick_and_place_at_a_site_with_no_plate_on_record_use_the_default_grip(
    arm_bench: _Bench,
) -> None:
    """Not refused: sent with no labware type, so the arm grips with its own default.
    The record moves no plate and the jaws stay empty."""
    sent = _arm_requests(arm_bench)

    await _adhoc(arm_bench, "pick_at_coords", _teachpoint("stacker"))
    await _adhoc(arm_bench, "place_at_coords", _teachpoint("stacker"))

    assert sent == [("pick_at_coords", ""), ("place_at_coords", "")]
    assert arm_bench.ledger_site() == "pad"
    arm = next(m for m in arm_bench.runtime.system.movers if m.name == arm_bench.lh_name)
    assert arm.labware is None
    assert await _commands_named(arm_bench, "pick_at_coords") == [arm_bench.lh_name]


@pytest.mark.timeout(30)
@pytest.mark.parametrize("command", ["initialize", "home", "is_connected"])
async def test_an_arm_command_with_no_labware_completes(
    arm_bench: _Bench, command: str,
) -> None:
    """A transporter is not a resource with a site, so nothing stands at it. The command is still on the record."""
    await record_operator_command(arm_bench.runtime.system, "arm", command, {}, interfaces=frozenset({"ITransporter"}))

    assert arm_bench.ledger_site() == "pad"
    recorded = [op for op in await _all_ops(arm_bench) if op.device_name == "arm" and op.source is TrackingSource.OPERATOR]
    assert [(op.operation, op.details) for op in recorded] == [
        (DeviceOperation.COMMAND, GenericOperationDetails(command=command, args_repr="{}")),
    ]


@pytest.mark.timeout(30)
async def test_an_adhoc_home_on_a_liquid_handler_is_recorded(tip_bench: _Bench) -> None:
    """A robotic movement is an operation even when it changes no labware."""
    await _adhoc(tip_bench, "home", {})

    assert [op.operation for op in await _all_ops(tip_bench) if op.details == GenericOperationDetails(command="home", args_repr="{}")] == [
        DeviceOperation.COMMAND,
    ]


@pytest.mark.timeout(30)
async def test_an_adhoc_arm_pick_is_recorded_as_well_as_followed(arm_bench: _Bench) -> None:
    await _adhoc(arm_bench, "pick_at_coords", _teachpoint("pad"))

    assert [
        op.operation for op in await _all_ops(arm_bench) if op.device_name == "arm" and op.source is TrackingSource.OPERATOR
    ] == [DeviceOperation.COMMAND]


async def _operator_reach_in(bench: _Bench, device_id: str, command: str, position_id: str) -> None:
    """An operator's pick or place, sent the way the operator surface sends it."""
    token = current_run_mode.set(WorkflowRunMode.LIVE)
    try:
        await _runner_on(bench, _wire_to(bench)).run_named(device_id, command, _teachpoint(position_id))
    finally:
        current_run_mode.reset(token)


@pytest.mark.timeout(30)
async def test_an_operator_deck_gripper_place_is_saved_once(bench: _Bench) -> None:
    """The gripper's place sends `move_plate` to its handler, and that call is the place, not a second command."""
    gripper = f"{bench.lh_name}/gripper"
    driver = bench.runtime.system.get_device(bench.lh_name).driver
    await _operator_reach_in(bench, gripper, "pick_at_coords", RESERVOIR_SITE)
    assert bench.ledger_site() == f"{gripper}/gripper", "the plate is in the gripper's jaws"
    assert bench.labware_name in await _driver_deck(bench), (
        "the handler's own gripper holds the plate on its deck; the pick must not take it off the driver"
    )
    with patch.object(driver, "move_plate", wraps=driver.move_plate) as move_plate:
        await _operator_reach_in(bench, gripper, "place_at_coords", WORKING_SITE)

    assert move_plate.await_count == 1

    moves = [
        (op.device_name, op.details.command, op.source) for op in await _all_ops(bench)
        if isinstance(op.details, GenericOperationDetails)
        and op.details.command in ("pick_at_coords", "place_at_coords", "move_plate")
    ]
    assert moves == [
        (gripper, "pick_at_coords", TrackingSource.OPERATOR),
        (gripper, "place_at_coords", TrackingSource.OPERATOR),
    ]
    assert bench.ledger_site() == WORKING_SITE


async def _driver_deck(bench: _Bench) -> set[str]:
    """The labware names the liquid handler's driver has on its deck."""
    driver = bench.runtime.system.get_device(bench.lh_name).driver
    state = await driver.get_deck_state(GetDeckStateRequest())
    return {resource.name for resource in state.labware}


async def _register_at(bench: _Bench, location: str) -> str:
    """A second plate, put down by hand at ``location``. Returns its name."""
    snapshot = await bench.runtime.labware.register(
        "sample_plate", location=location, confirm=True,
    )
    return next(
        lw.name for lw in await bench.runtime.labware.list_all()
        if lw.id == snapshot.id
    )


@pytest.mark.timeout(30)
async def test_an_adhoc_pick_at_a_device_named_teachpoint_finds_the_plate(
    arm_bench: _Bench,
) -> None:
    """A teachpoint named after a device is the normal handoff shape.

    The device name is the reservation mutex, which is off the routing graph
    and never holds labware. Reading the pick position off it finds nothing
    there, so the operator's pick records nothing at all, and a place there
    parks the plate on the mutex, where the occupancy check then refuses
    every action on that device for the life of the process.
    """
    handoff = await _register_at(arm_bench, "stacker")
    mutex = arm_bench.runtime.system.system_map.get_location("stacker")
    assert arm_bench.site_of(handoff) == "stacker/slot"

    await _adhoc(arm_bench, "pick_at_coords", _teachpoint("stacker"))

    assert arm_bench.site_of(handoff) == "arm/gripper"
    assert mutex.labware is None

    await _adhoc(arm_bench, "place_at_coords", _teachpoint("stacker"))

    assert arm_bench.site_of(handoff) == "stacker/slot"
    assert mutex.labware is None, (
        "the mutex is not a site; a plate recorded on it blocks every "
        "reservation of that device until the process restarts"
    )


@pytest.mark.timeout(30)
async def test_a_second_pick_onto_full_jaws_is_refused_before_the_arm_moves(
    arm_bench: _Bench,
) -> None:
    """The arm's own holding guard refuses it, as it refuses a run's: the jaws would close on a second plate."""
    second = await _register_at(arm_bench, "stacker")
    await _adhoc(arm_bench, "pick_at_coords", _teachpoint("pad"))
    assert arm_bench.ledger_site() == "arm/gripper"

    with pytest.raises(MoverAlreadyHoldingError):
        await _adhoc(arm_bench, "pick_at_coords", _teachpoint("stacker"))

    assert arm_bench.ledger_site() == "arm/gripper", (
        "the record that was already there is not overwritten"
    )
    assert arm_bench.site_of(second) == "stacker/slot", (
        "a refused claim leaves the source alone so the operator can re-issue"
    )


@pytest.mark.timeout(30)
async def test_a_pick_whose_record_fails_leaves_the_plate_where_the_arm_put_it(
    arm_bench: _Bench,
) -> None:
    """The arm moved, so the record says where the arm left the plate, and the command fails saying it ran.

    The same as a run's pick whose save fails (record not saved). The ledger holds one position.
    """
    with patch.object(
        operator_recorder, "save_outside_a_run",
        AsyncMock(side_effect=RuntimeError("the observer wire went down")),
    ), pytest.raises(CommandNotSavedError, match="observer wire.*do not send it again"):
        await _adhoc(arm_bench, "pick_at_coords", _teachpoint("pad"))

    assert arm_bench.ledger_site() == "arm/gripper"


@dataclass(frozen=True)
class _Batch:
    results: list[BatchCommandResultDTO]

    @property
    def success(self) -> bool:
        return all(r.success for r in self.results)


def _runner_on(bench: _Bench, execute_command: Callable[..., Awaitable[Any]]) -> OperatorDeviceCommands:
    tracker_snapshot = MagicMock()
    tracker_snapshot.interfaces = ["ILiquidHandler"]
    tracker = MagicMock()
    controller = MagicMock()
    tracker.get_device = AsyncMock(return_value=tracker_snapshot)
    controller.execute_command = AsyncMock(side_effect=execute_command)
    return OperatorDeviceCommands(
        gateway_with(tracker=tracker, controller=controller), lambda: bench.runtime)


async def _batched(
    bench: _Bench, command: str, params: dict[str, Any], *, times: int = 1, stop_on_error: bool = True,
) -> _Batch:
    """The same command, sent the way the batch route sends it."""
    token = current_run_mode.set(WorkflowRunMode.LIVE)
    try:
        results, _, _ = await run_batch(
            NamedCommands(bench.runtime, _runner_on(bench, _wire_to(bench)), bench.lh_name),
            [BatchCommand(command=command, params=params)] * times, stop_on_error=stop_on_error, reason=None,
        )
    finally:
        current_run_mode.reset(token)
    return _Batch(results)


@pytest.mark.timeout(30)
async def test_a_batched_command_whose_save_fails_says_it_ran(arm_bench: _Bench) -> None:
    """A batch reports the failed save as its own error, so nobody resends a command that ran."""
    with patch.object(
        operator_recorder, "save_outside_a_run",
        AsyncMock(side_effect=RuntimeError("the observer wire went down")),
    ):
        result = await _batched(arm_bench, "pick_at_coords", _teachpoint("pad"))

    assert not result.success
    assert result.results[0].error_type == "CommandNotSavedError"


@pytest.mark.timeout(30)
async def test_a_batch_told_to_keep_going_still_stops_after_a_command_it_could_not_save(arm_bench: _Bench) -> None:
    """Every later command would act on a record that is missing the one that ran."""
    with patch.object(
        operator_recorder, "save_outside_a_run",
        AsyncMock(side_effect=RuntimeError("the observer wire went down")),
    ):
        result = await _batched(arm_bench, "pick_at_coords", _teachpoint("pad"), times=2, stop_on_error=False)

    assert [r.error_type for r in result.results] == ["CommandNotSavedError"]


@pytest.mark.timeout(30)
async def test_a_batched_move_leaves_the_same_trail_as_a_single_one(
    bench: _Bench,
) -> None:
    """Batch is the only by-name device route, so it is the one an operator
    reaches for a command with no typed endpoint -- and it dispatched straight
    at the controller, skipping the recorder every other operator path uses.
    The ledger then answered with the pre-command site, and the next deck
    reconcile pushed that back down over the driver.
    """
    assert bench.ledger_site() == RESERVOIR_SITE

    result = await _batched(bench, "move_plate", {
        "plate": bench.labware_name,
        "to_position": WORKING_SITE.split("/", 1)[1],
        "from_position": RESERVOIR_SITE.split("/", 1)[1],
    })
    assert result.success, result.results[0].error

    assert bench.ledger_site() == WORKING_SITE, (
        "a command sent through the batch route moved the plate on the "
        "instrument; a ledger still naming the source is the answer every "
        "surface gives"
    )


@pytest.mark.timeout(30)
async def test_a_batch_holds_external_control_for_its_whole_run(
    bench: _Bench,
) -> None:
    """Taken per command, the engine could schedule the same device between two
    of the operator's own commands -- exactly the window a batch exists to
    avoid. The flag goes up once and comes down at the end.
    """
    device = bench.runtime.system.get_device(bench.lh_name)
    seen: list[bool] = []

    async def watching(**kwargs: Any) -> Any:
        seen.append(device.under_external_control)
        return await _wire_to(bench)(**kwargs)

    token = current_run_mode.set(WorkflowRunMode.LIVE)
    try:
        await run_batch(
            NamedCommands(bench.runtime, _runner_on(bench, watching), bench.lh_name),
            [BatchCommand(command="get_deck_state"), BatchCommand(command="get_deck_state")],
            stop_on_error=True, reason=None,
        )
    finally:
        current_run_mode.reset(token)

    assert seen == [True, True], "the engine could reach the device mid-batch"
    assert not device.under_external_control, "the flag was left up"


@pytest.mark.timeout(30)
async def test_an_adhoc_pick_records_the_channels_the_operator_named(
    tip_bench: _Bench,
) -> None:
    """`use_channels` is on the wire and the recorder used to drop it.

    The operator names the channels, the robot uses them, and the record filed
    the tips from channel zero instead and marked every number a guess. The
    read then contradicted the command the operator had just sent.
    """
    runtime = tip_bench.runtime
    await runtime.labware.set_tip_state(
        tip_bench.labware_id, ["A1", "B1"], reason="bench setup", confirm=True,
    )

    await _adhoc(tip_bench, "pick_up_tips", {
        "picks": [{"tip_rack": tip_bench.labware_name, "positions": ["A1"]}],
        "use_channels": [3],
    })

    carrying = await runtime.devices.get_mounted_tips(tip_bench.lh_name)
    assert list(carrying.by_channel) == [3], (
        f"the operator named channel 3; the record says {list(carrying.by_channel)}"
    )
    assert carrying.by_channel[3].channel_is_inferred is False


@pytest.mark.timeout(30)
async def test_an_adhoc_discard_leaves_the_head_empty(tip_bench: _Bench) -> None:
    """A manual discard takes the tips off the head, and the record follows.

    The workflow path writes a discard record for exactly this reason: a
    discard names no rack, so without its own record nothing ever takes the
    tips off the head and it reports them mounted forever. The operator path
    parsed the same command and wrote nothing, so the two paths disagreed about
    a head an operator had just emptied.
    """
    runtime = tip_bench.runtime
    await runtime.labware.set_tip_state(
        tip_bench.labware_id, ["A1", "B1"], reason="bench setup", confirm=True,
    )
    await _adhoc(tip_bench, "pick_up_tips", {
        "picks": [{"tip_rack": tip_bench.labware_name, "positions": ["A1"]}],
    })
    carrying = await runtime.devices.get_mounted_tips(tip_bench.lh_name)
    assert carrying.by_channel, "the pick-up must put a tip on the head first"

    await _adhoc(tip_bench, "discard_tips", {})

    after = await runtime.devices.get_mounted_tips(tip_bench.lh_name)
    assert not after.by_channel, (
        "the tips went to waste; a head that still reports them mounted "
        f"refuses the next pick-up. Still carrying: {after.by_channel}"
    )


@pytest.mark.timeout(30)
async def test_an_adhoc_drop_to_waste_leaves_the_head_empty(
    tip_bench: _Bench,
) -> None:
    """Dropping tips to waste is the same event as a discard, and records.

    `DropTipsRequest` defaults to `to_waste=True` with no `drops`, which is the
    shape an operator sends to bin the tips. The recorder walked `drops` and
    produced nothing for it, so the head reported tips that were in the trash.
    Same defect the discard above fixes, on the more common command.
    """
    runtime = tip_bench.runtime
    await runtime.labware.set_tip_state(
        tip_bench.labware_id, ["A1", "B1"], reason="bench setup", confirm=True,
    )
    await _adhoc(tip_bench, "pick_up_tips", {
        "picks": [{"tip_rack": tip_bench.labware_name, "positions": ["A1"]}],
    })
    carrying = await runtime.devices.get_mounted_tips(tip_bench.lh_name)
    assert carrying.by_channel, "the pick-up must put a tip on the head first"

    await _adhoc(tip_bench, "drop_tips", {"to_waste": True})

    after = await runtime.devices.get_mounted_tips(tip_bench.lh_name)
    assert not after.by_channel, (
        "the tips went to waste; a head that still reports them mounted "
        f"refuses the next pick-up. Still carrying: {after.by_channel}"
    )


@pytest.mark.timeout(30)
async def test_a_move_of_a_plate_with_no_position_yet_is_recorded(
    bench: _Bench,
) -> None:
    """A plate the ledger expects but has never placed is still followed.

    An expected labware has no position to move it off, and the recorder handed
    the relocation a source of None. It raised, and the recorder swallows what
    it cannot record, so the move reached the deck and the ledger kept the
    position from before it -- which for an expected plate is no position at
    all.
    """
    runtime = bench.runtime
    expected = await runtime.labware.register(
        "reservoir", location=None, confirm=True,
    )
    name = next(
        lw.name for lw in await runtime.labware.list_all() if lw.id == expected.id
    )

    instance = runtime.system.get_labware(name)
    target = runtime.system.system_map.get_location(WORKING_SITE)
    await runtime.system.command_pipeline.run(
        bench.lh_name, "move_plate", _nothing_sent, _nothing_recorded,
        statement=device_move(bench.lh_name, instance, None, target), checks=(), params=None,
        execution_id=None, events=None, carries_out=None,
    )

    landed = next(
        lw for lw in await runtime.labware.list_all() if lw.id == expected.id
    )
    assert landed.current_location == WORKING_SITE, (
        "the operator put the plate on the deck; the ledger has it at "
        f"{landed.current_location!r}"
    )
    assert landed.placement is PlacementState.PRESENT, (
        "an expected plate an operator has put down is present, not still expected"
    )


class TestSplittingChannelsAcrossRackSlices:
    """One call slices across racks and writes a record per rack, but the
    channels engage in target order across the whole call. A slice that counted
    from zero on its own claimed channels the slice before it already held, and
    the fold dropped those tips."""

    @staticmethod
    def _details(request: PickUpTipsRequest | DropTipsRequest) -> list[OperationDetails]:
        context = RecordContext(
            device_name="lh", action_id="a", thread_id="t",
            source=TrackingSource.OPERATOR, resolve_labware_id=lambda _name: None,
        )
        return [op.details for op in operations_for_request(request, context)]

    @classmethod
    def _picked(cls, request: PickUpTipsRequest) -> list[tuple[list[int] | None, bool]]:
        return [
            (details.use_channels, details.channels_were_counted)
            for details in cls._details(request)
            if isinstance(details, TipPickUpDetails)
        ]

    def test_each_slice_takes_the_channels_the_operator_named_for_it(self) -> None:
        request = PickUpTipsRequest(
            picks=[
                TipPick(tip_rack="tips_1", positions=["A1", "B1"]),
                TipPick(tip_rack="tips_2", positions=["C1", "D1"]),
            ],
            use_channels=[0, 2, 4, 6],
        )
        assert self._picked(request) == [([0, 2], False), ([4, 6], False)]

    def test_naming_no_channels_still_counts_them_across_the_whole_call(self) -> None:
        """The second rack used to be filed on channels 0 and 1 again, so the
        fold overwrote the first rack's tips and the head lost half of them."""
        request = PickUpTipsRequest(
            picks=[
                TipPick(tip_rack="tips_1", positions=["A1", "B1"]),
                TipPick(tip_rack="tips_2", positions=["C1", "D1"]),
            ],
        )
        assert self._picked(request) == [([0, 1], True), ([2, 3], True)]

    def test_a_count_that_does_not_match_is_counted_instead(self) -> None:
        """Pairing them anyway makes every later read of that head raise
        `MalformedTipRecord` instead of answering."""
        request = PickUpTipsRequest(
            picks=[TipPick(tip_rack="tips_1", positions=["A1", "B1"])],
            use_channels=[0],
        )
        assert self._picked(request) == [([0, 1], True)]

    def test_a_return_to_racks_keeps_the_channels_it_emptied(self) -> None:
        request = DropTipsRequest(
            drops=[
                TipPick(tip_rack="tips_1", positions=["A1"]),
                TipPick(tip_rack="tips_2", positions=["C1"]),
            ],
            to_waste=False, use_channels=[5, 7],
        )
        emptied = [
            details.use_channels
            for details in self._details(request)
            if isinstance(details, TipDropDetails)
        ]
        assert emptied == [[5], [7]]


class TestAnUnnamedPickStillLandsOnTheHead:
    """One rack, no channels named. Pins the fold's own reading of a counted
    record; the multi-rack case it used to lose is pinned on the fold itself in
    `tests/test_the_record_says_what_a_head_carries.py`."""

    async def test_the_tips_land_and_say_the_channels_were_counted(
        self, tip_bench: _Bench,
    ) -> None:
        runtime = tip_bench.runtime
        await runtime.labware.set_tip_state(
            tip_bench.labware_id, ["A1", "B1"], reason="bench setup", confirm=True,
        )

        await _adhoc(tip_bench, "pick_up_tips", {
            "picks": [{"tip_rack": tip_bench.labware_name, "positions": ["A1", "B1"]}],
        })

        carrying = await runtime.devices.get_mounted_tips(tip_bench.lh_name)
        assert sorted(carrying.by_channel) == [0, 1]
        assert all(tip.channel_is_inferred for tip in carrying.by_channel.values())


async def _all_ops(bench: _Bench) -> list[OperationRecord]:
    """Every operation the record holds, across every bucket."""
    hits = await bench.runtime.system.ops_history.store.search(OpsHistorySearchQuery())
    return [op for _execution_id, record in hits for op in record.operations]


@pytest.mark.timeout(30)
async def test_an_adhoc_command_keeps_what_the_driver_answered(
    tip_bench: _Bench,
) -> None:
    """The driver's own report of the command is kept, not thrown away.

    A workflow action keeps that report as a witness to compare its own record
    against. The operator path had the same report in hand and dropped it, so
    the one path where a person is standing at the instrument was the one path
    with nothing to compare.
    """
    runtime = tip_bench.runtime
    await runtime.labware.set_tip_state(
        tip_bench.labware_id, ["A1", "B1"], reason="bench setup", confirm=True,
    )

    await _adhoc(tip_bench, "pick_up_tips", {
        "picks": [{"tip_rack": tip_bench.labware_name, "positions": ["A1"]}],
    })

    witnessed = [
        op for op in await _all_ops(tip_bench)
        if op.source is TrackingSource.DRIVER_OBSERVED
    ]
    assert witnessed, (
        "the driver reported its racks back on the pick and nothing kept it"
    )
    assert any(tip_bench.labware_name in op.affected_labware for op in witnessed)


@pytest.mark.timeout(30)
async def test_an_adhoc_aspirate_with_a_failed_channel_is_saved_per_channel(tip_bench: _Bench) -> None:
    """The same record a workflow's aspirate leaves: the failed channel moved nothing, and the command fails."""
    with pytest.raises(PartialChannelFailureError):
        await record_operator_command(
            tip_bench.runtime.system, tip_bench.lh_name, "aspirate",
            {"aspirations": [{"labware": "plate_p", "positions": ["A1", "B1"], "volumes": [10.0, 10.0]}]},
            result={"success": False, "per_channel_errors": [{
                "channel_id": 1, "well_position": "B1", "labware": "plate_p", "attempted_volume": 10.0,
                "error_code": "no_liquid", "error_message": "no liquid found",
            }]},
            interfaces=frozenset({"ILiquidHandler"}),
        )

    saved = [op.details for op in await _all_ops(tip_bench) if op.operation is DeviceOperation.ASPIRATE]
    assert [(d.positions, d.volumes, d.certainty) for d in saved if isinstance(d, AspirateDetails)] == [
        (["A1"], [10.0], "confirmed_transferred"),
        (["B1"], [0.0], "definitely_not_transferred"),
    ]
    assert all(op.source is TrackingSource.OPERATOR for op in await _all_ops(tip_bench) if op.operation is DeviceOperation.ASPIRATE)


def _failed_tip_channel(rack: str, position: str | None, channel: int) -> dict[str, JsonValue]:
    return {
        "channel_id": channel, "well_position": position, "labware": rack, "attempted_volume": 0.0,
        "error_code": "TipNotMoved", "error_message": "the channel did not go",
    }


@pytest.mark.timeout(30)
async def test_an_adhoc_tip_pickup_with_a_failed_channel_takes_only_the_tips_that_went(tip_bench: _Bench) -> None:
    """Three channels were sent and one got no tip: its tip stays in the rack, its channel stays bare, and the command fails."""
    runtime = tip_bench.runtime
    rack = tip_bench.labware_name
    await runtime.labware.set_tip_state(tip_bench.labware_id, ["A1", "B1", "C1"], reason="bench setup", confirm=True)

    with pytest.raises(PartialChannelFailureError):
        await record_operator_command(
            runtime.system, tip_bench.lh_name, "pick_up_tips",
            {"picks": [{"tip_rack": rack, "positions": ["A1", "B1", "C1"]}]},
            result={"success": False, "per_channel_errors": [_failed_tip_channel(rack, "B1", 1)]},
            interfaces=frozenset({"ILiquidHandler"}),
        )

    assert (await runtime.labware.get_tip_state(tip_bench.labware_id)).positions_present == ["B1"]
    assert sorted((await runtime.devices.get_mounted_tips(tip_bench.lh_name)).by_channel) == [0, 2]


@pytest.mark.timeout(30)
async def test_an_adhoc_tip_drop_with_a_failed_channel_returns_only_the_tips_that_went(tip_bench: _Bench) -> None:
    """Two channels were sent back to the rack and one kept its tip: the rack gets one tip back, and the command fails."""
    runtime = tip_bench.runtime
    rack = tip_bench.labware_name
    await runtime.labware.set_tip_state(tip_bench.labware_id, ["A1", "B1"], reason="bench setup", confirm=True)
    await _adhoc(tip_bench, "pick_up_tips", {"picks": [{"tip_rack": rack, "positions": ["A1", "B1"]}]})

    with pytest.raises(PartialChannelFailureError):
        await record_operator_command(
            runtime.system, tip_bench.lh_name, "drop_tips",
            {"drops": [{"tip_rack": rack, "positions": ["A1", "B1"]}], "to_waste": False},
            result={"success": False, "per_channel_errors": [_failed_tip_channel(rack, "B1", 1)]},
            interfaces=frozenset({"ILiquidHandler"}),
        )

    assert (await runtime.labware.get_tip_state(tip_bench.labware_id)).positions_present == ["A1"]
    assert sorted((await runtime.devices.get_mounted_tips(tip_bench.lh_name)).by_channel) == [1]


@pytest.mark.timeout(30)
async def test_an_adhoc_discard_with_a_failed_channel_bins_only_the_tips_that_went(tip_bench: _Bench) -> None:
    """Two named channels were sent to waste and one kept its tip: the head still carries that one."""
    runtime = tip_bench.runtime
    rack = tip_bench.labware_name
    await runtime.labware.set_tip_state(tip_bench.labware_id, ["A1", "B1"], reason="bench setup", confirm=True)
    await _adhoc(tip_bench, "pick_up_tips", {"picks": [{"tip_rack": rack, "positions": ["A1", "B1"]}]})

    with pytest.raises(PartialChannelFailureError):
        await record_operator_command(
            runtime.system, tip_bench.lh_name, "discard_tips", {"use_channels": [0, 1]},
            result={"success": False, "per_channel_errors": [_failed_tip_channel("trash", None, 1)]},
            interfaces=frozenset({"ILiquidHandler"}),
        )

    assert sorted((await runtime.devices.get_mounted_tips(tip_bench.lh_name)).by_channel) == [1]


@pytest.mark.timeout(30)
async def test_a_discard_naming_no_channels_with_a_failed_channel_writes_no_row(tip_bench: _Bench) -> None:
    """Nothing says which channels let go, so no row is written: one row for the failed channel alone would leave
    the others reading as known on the head."""
    runtime = tip_bench.runtime
    rack = tip_bench.labware_name
    await runtime.labware.set_tip_state(tip_bench.labware_id, ["A1", "B1"], reason="bench setup", confirm=True)
    await _adhoc(tip_bench, "pick_up_tips", {"picks": [{"tip_rack": rack, "positions": ["A1", "B1"]}]})

    with pytest.raises(ChannelFailureNotReadError):
        await record_operator_command(
            runtime.system, tip_bench.lh_name, "discard_tips", {},
            result={"success": False, "per_channel_errors": [_failed_tip_channel("trash", None, 1)]},
            interfaces=frozenset({"ILiquidHandler"}),
        )

    assert not [op for op in await _all_ops(tip_bench) if op.operation is DeviceOperation.DISCARD_TIPS]
    assert sorted((await runtime.devices.get_mounted_tips(tip_bench.lh_name)).by_channel) == [0, 1]


async def _failed_channel_not_read(_answer: None) -> None:
    raise ChannelFailureNotReadError("discard_tips")


@pytest.mark.timeout(30)
async def test_a_channel_failure_nothing_can_read_fails_the_command_as_a_failed_channel() -> None:
    """The pipeline answers it as a failed channel with no rows, so a run does not send it again and an operator
    gets `channel_failed`."""
    pipeline: DeviceCommandPipeline[CommandStatement] = DeviceCommandPipeline(())

    with pytest.raises(PartialChannelFailureError) as failed:
        await pipeline.run_device_command(
            "lh", "discard_tips", _nothing_sent, _failed_channel_not_read, checks=(), execution_id=None, named=(), params=None,
        )

    assert failed.value.records == []


@pytest.mark.timeout(30)
async def test_an_adhoc_96_head_pick_up_and_return_show_on_the_head(tip_bench: _Bench) -> None:
    """A 96-head pick-up by hand loads the 96 head on the record, and a return takes the tips off again."""
    runtime = tip_bench.runtime
    lh = frozenset({"ILiquidHandler"})

    await record_operator_command(
        runtime.system, tip_bench.lh_name, "pick_up_tips96", {"tip_rack": tip_bench.labware_name}, interfaces=lh,
    )
    loaded = await runtime.devices.get_mounted_tips(tip_bench.lh_name)
    assert len(loaded.head96) == 96
    assert {tip.tip_rack for tip in loaded.head96.values()} == {tip_bench.labware_name}
    assert loaded.by_channel == {}

    await record_operator_command(runtime.system, tip_bench.lh_name, "return_tips96", {}, interfaces=lh)
    assert (await runtime.devices.get_mounted_tips(tip_bench.lh_name)).head96 == {}


@pytest.mark.timeout(30)
async def test_a_cut_off_96_head_pick_up_keeps_the_head_in_doubt_through_a_channel_discard(tip_bench: _Bench) -> None:
    """Nothing ever mentioned this handler's 96 head. A 96-head pick-up gets no answer, then the channels are
    discarded. The head may hold 96 tips, so the read stays stale."""
    runtime = tip_bench.runtime

    await runtime.system.note_no_answer(tip_bench.lh_name, None, [tip_bench.labware_name], "pick_up_tips96")
    await record_operator_command(
        runtime.system, tip_bench.lh_name, "discard_tips", {}, interfaces=frozenset({"ILiquidHandler"}),
    )

    assert (await runtime.devices.get_mounted_tips(tip_bench.lh_name)).provenance is Provenance.STALE


@pytest.mark.timeout(30)
async def test_a_96_head_return_puts_the_tips_back_in_the_rack_they_came_from(tip_bench: _Bench) -> None:
    """A full rack is picked up by the 96 head and returned: the rack reads full and known again, and the next
    96-head pick-up from it is accepted."""
    runtime = tip_bench.runtime
    lh = frozenset({"ILiquidHandler"})
    rack = runtime.system.get_labware(tip_bench.labware_name)
    every_spot = sorted(rack.every_tip_position())
    await runtime.labware.set_tip_state(tip_bench.labware_id, every_spot, reason="a full rack", confirm=True)
    pick_up = {"tip_rack": tip_bench.labware_name}

    await record_operator_command(runtime.system, tip_bench.lh_name, "pick_up_tips96", pick_up, interfaces=lh)
    assert (await runtime.labware.get_tip_state(tip_bench.labware_id)).positions_present == []

    await record_operator_command(runtime.system, tip_bench.lh_name, "return_tips96", {}, interfaces=lh)

    back = await runtime.labware.get_tip_state(tip_bench.labware_id)
    assert sorted(back.positions_present) == every_spot
    assert back.provenance is Provenance.KNOWN
    await operator_recorder.check_before_sending(runtime.system, "pick_up_tips96", pick_up, lh)


def _stated_on_the_96_head(rack: str, spots: list[str]) -> dict[int, tuple[str, str]]:
    return {channel: (rack, spot) for channel, spot in enumerate(spots)}


@pytest.mark.timeout(30)
async def test_a_96_head_return_credits_the_spots_the_head_holds_not_the_racks_last_layout(tip_bench: _Bench) -> None:
    """The rack was stated at 56 tips and the 96 head as holding the other 40 from it. The return puts those 40
    spots back, so the rack reads all 96."""
    runtime = tip_bench.runtime
    lh = frozenset({"ILiquidHandler"})
    every_spot = sorted(runtime.system.get_labware(tip_bench.labware_name).every_tip_position())
    in_the_rack, on_the_head = every_spot[:56], every_spot[56:]
    await runtime.labware.set_tip_state(tip_bench.labware_id, in_the_rack, reason="56 left", confirm=True)
    await runtime.devices.set_mounted_tips(
        tip_bench.lh_name, {}, head96=_stated_on_the_96_head(tip_bench.labware_name, on_the_head),
        reason="40 on the 96 head", confirm=True,
    )

    await record_operator_command(runtime.system, tip_bench.lh_name, "return_tips96", {}, interfaces=lh)

    back = await runtime.labware.get_tip_state(tip_bench.labware_id)
    assert sorted(back.positions_present) == every_spot
    assert (await runtime.devices.get_mounted_tips(tip_bench.lh_name)).head96 == {}


@pytest.mark.timeout(30)
async def test_a_96_head_return_of_tips_from_two_racks_credits_each_rack_its_own_spots(tip_bench: _Bench) -> None:
    """A head stated as holding tips from two racks returns each tip to the rack and spot it came from."""
    runtime = tip_bench.runtime
    lh = frozenset({"ILiquidHandler"})
    first = tip_bench.labware_name
    await runtime.labware.set_tip_state(tip_bench.labware_id, ["C1"], reason="one left", confirm=True)
    await runtime.devices.set_mounted_tips(
        tip_bench.lh_name, {}, head96={0: (first, "A1"), 1: (first, "B1"), 8: ("another_rack", "A2")},
        reason="two racks", confirm=True,
    )

    await record_operator_command(runtime.system, tip_bench.lh_name, "return_tips96", {}, interfaces=lh)

    assert sorted((await runtime.labware.get_tip_state(tip_bench.labware_id)).positions_present) == ["A1", "B1", "C1"]
    returns = [
        op.details for op in await _all_ops(tip_bench)
        if op.operation is DeviceOperation.RETURN_TIPS96 and isinstance(op.details, TipDrop96Details)
    ]
    assert sorted((d.tip_rack or "", d.positions or []) for d in returns) == [
        ("another_rack", ["A2"]), (first, ["A1", "B1"]),
    ]


@pytest.mark.timeout(30)
async def test_a_runs_96_head_return_puts_the_tips_back_in_the_rack(tip_bench: _Bench) -> None:
    """The same through a run's save point: the action's recorder names the rack from the record of the head."""
    runtime = tip_bench.runtime
    system = runtime.system
    lh = frozenset({"ILiquidHandler"})
    every_spot = sorted(system.get_labware(tip_bench.labware_name).every_tip_position())
    await runtime.labware.set_tip_state(tip_bench.labware_id, every_spot, reason="a full rack", confirm=True)
    await record_operator_command(
        system, tip_bench.lh_name, "pick_up_tips96", {"tip_rack": tip_bench.labware_name}, interfaces=lh,
    )
    action = Mock(id="action_1", command="return_tips96", device=system.get_device(tip_bench.lh_name), declares=None)
    action.build_template_to_instance_map.return_value = {}
    executable = ExecutableLocationAction(
        Mock(), action,
        MethodExecutionContext(
            execution_id="run_1", workflow_name="return", method_id="m1", method_name="method",
            thread_id="thread_1", thread_name="thread_1", participating_thread_ids=("thread_1",),
        ),
        tracking_context=system.tracking_context,
    )
    executable._wire_recorder()
    recorder: CommandRecorder = action.set_recorder.call_args[0][0]

    await recorder.save_completed(
        ActionRequest(device_name=tip_bench.lh_name, command="return_tips96", args=(), kwargs={}), None, [], [],
    )

    back = await runtime.labware.get_tip_state(tip_bench.labware_id)
    assert sorted(back.positions_present) == every_spot
    assert (await runtime.devices.get_mounted_tips(tip_bench.lh_name)).head96 == {}


@pytest.mark.timeout(30)
async def test_an_adhoc_mix_leaves_the_record_a_workflow_mix_leaves(
    tip_bench: _Bench,
) -> None:
    """One reader, so a verb cannot be tracked on one path and not the other.

    Mix was read on the workflow path and skipped on the operator one, because
    two readers would have written two shapes for it. There is one reader now,
    so there is one shape.
    """
    runtime = tip_bench.runtime
    await runtime.labware.set_tip_state(
        tip_bench.labware_id, ["A1"], reason="bench setup", confirm=True,
    )
    reservoir = await runtime.labware.register(
        "reservoir", location=RESERVOIR_SITE, confirm=True,
    )
    reservoir_name = next(
        lw.name for lw in await runtime.labware.list_all() if lw.id == reservoir.id
    )
    await _adhoc(tip_bench, "pick_up_tips", {
        "picks": [{"tip_rack": tip_bench.labware_name, "positions": ["A1"]}],
    })

    await _adhoc(tip_bench, "mix", {
        "labware": reservoir_name,
        "volume": 50.0,
        "repetitions": 3,
        "positions": ["A1"],
    })

    mixes = [
        op for op in await _all_ops(tip_bench)
        if op.operation is DeviceOperation.MIX
    ]
    assert len(mixes) == 1, f"expected one mix in the record, found {len(mixes)}"
    assert mixes[0].source is TrackingSource.OPERATOR
    assert mixes[0].affected_labware == [reservoir_name]


def _watching() -> tuple[ServerEvents, CollectorSink]:
    sink = CollectorSink()
    events = ServerEvents()
    events.add_sink(sink)
    return events, sink


def _moves_of(sink: CollectorSink, labware_id: str) -> list[str | None]:
    return [
        e.context.position_id for e in sink.events
        if isinstance(e.context, LabwareRecordContext) and e.entity_id == labware_id and e.status == "MOVED"
    ]


@pytest.mark.timeout(30)
async def test_an_operator_pick_and_place_each_announce_where_the_plate_went() -> None:
    events, sink = _watching()
    async with running_arm_bench(events) as bench:
        sink.clear()

        await _adhoc(bench, "pick_at_coords", _teachpoint("pad"))
        picked = _moves_of(sink, bench.labware_id)
        await _adhoc(bench, "place_at_coords", _teachpoint("stacker"))

        assert picked == ["arm/gripper"]
        assert _moves_of(sink, bench.labware_id)[1:] == [bench.ledger_site()]


@pytest.mark.timeout(30)
async def test_an_operator_pick_records_the_position_it_reached_and_not_the_keys_the_reach_ignored() -> None:
    async with running_arm_bench(ServerEvents()) as bench:
        await _adhoc(bench, "pick_at_coords", {**_teachpoint("pad"), "speed": "fast"})

        picks = [
            op.details for op in await _all_ops(bench)
            if isinstance(op.details, GenericOperationDetails) and op.details.command == "pick_at_coords"
        ]
        assert [pick.args_repr for pick in picks] == [repr(_teachpoint("pad"))]


@pytest.mark.timeout(30)
async def test_an_operator_plate_move_announces_the_plate_it_names() -> None:
    events, sink = _watching()
    async with running_lh_bench(events) as bench:
        sink.clear()

        await _adhoc(bench, "move_plate", {
            "plate": bench.labware_name,
            "to_position": WORKING_SITE.split("/", 1)[1],
            "from_position": RESERVOIR_SITE.split("/", 1)[1],
        })

        assert _moves_of(sink, bench.labware_id) == [WORKING_SITE]


@pytest.mark.timeout(30)
async def test_an_operator_command_whose_params_do_not_fit_is_refused_as_invalid(tip_bench: _Bench) -> None:
    """Its labware motion and tip check are read before it is sent, so bad params are refused there, as invalid."""
    with pytest.raises(InvalidCommandError, match="aspirate"):
        await _adhoc(tip_bench, "aspirate", {"aspirations": "not a list"})


@pytest.mark.timeout(30)
async def test_an_operator_place_onto_a_deck_site_the_record_fills_is_refused_before_the_arm_moves(
    bench: _Bench,
) -> None:
    gripper = f"{bench.lh_name}/gripper"
    driver = bench.runtime.system.get_device(bench.lh_name).driver
    await bench.runtime.labware.register("reservoir", location=WORKING_SITE, confirm=True)
    await _operator_reach_in(bench, gripper, "pick_at_coords", RESERVOIR_SITE)

    with patch.object(driver, "move_plate", wraps=driver.move_plate) as move_plate, pytest.raises(
        ValueError, match="already contains labware",
    ):
        await _operator_reach_in(bench, gripper, "place_at_coords", WORKING_SITE)

    assert move_plate.await_count == 0
    assert bench.ledger_site() != WORKING_SITE
