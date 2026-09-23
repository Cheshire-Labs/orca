"""An operator's own device command has to leave the same trail a workflow's does.

Two dispatch paths reach a device: a workflow action, which runs the operation
interpreter and writes the ledger, and an ad-hoc command, which goes straight to
the driver. Only the first recorded anything, so every manual move, pick-up and
dispense an operator made during a recovery was invisible to the ledger -- and
the ledger is both what every operator surface reads AND what the next deck
reconcile pushes back down over the driver.
"""

from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.liquid_handler_models import (
    DropTipsRequest,
    GetDeckStateRequest,
    PickUpTipsRequest,
    TipPick,
)
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from orca.gateway import adhoc
from orca.gateway.batch import BatchExecutor, DeviceCommand
from orca.runtime.incident_store import (
    DeckReconcileConflictDetail,
    IncidentCategory,
    LedgerContradictionDetail,
)
from orca.runtime.adhoc_ledger import _details_for, record_operator_command
from orca.resource_models.labware_location_service import PlacementState
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.state.provenance import Provenance
from orca.state.records import (
    ObservationGapCause,
    TipDropDetails,
    TipPickUpDetails,
)
from orca.runtime.run_modes import WorkflowRunMode, current_run_mode
from orca.runtime.system_runtime import SystemRuntime
from tests.test_deck_resident_reagent_pipetting import build_pipetting_bench
from tests.test_labware_state_reconciliation import RESERVOIR_SITE, _build

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
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, lh = await _build(recorder, wf_name="adhoc_recording")
    runtime = SystemRuntime(
        build.system, event_bus=build.event_bus, labware_store=InMemoryLabwareStore(),
    )
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


def _wire_to(bench: _Bench):
    """Stand in for the WebSocket only: run the command on the real driver."""

    async def execute_command(*, device_id: str, command: str,
                              params: dict[str, Any], **_: Any) -> Any:
        from cheshire_drivers.lh_request_validation import (
            LH_REQUEST_MODELS, wrap_lh_payload,
        )
        if command not in LH_REQUEST_MODELS:
            # An arm command under external control: the driver validates and
            # mutates nothing by contract, so there is no driver state to drive.
            return None
        device = bench.runtime.system.get_device(bench.lh_name)
        result = await getattr(device.driver, command)(
            **wrap_lh_payload(command, params)
        )
        return result.model_dump(mode="json") if result is not None else None

    return execute_command


async def _adhoc(bench: _Bench, command: str, params: dict[str, Any]) -> None:
    """Dispatch as an operator's own command, in the world an operator is in."""
    tracker_snapshot = MagicMock()
    tracker_snapshot.interfaces = ["ILiquidHandler"]
    token = current_run_mode.set(WorkflowRunMode.LIVE)
    try:
        with patch("orca.gateway.adhoc.device_connection_tracker") as tracker, \
             patch("orca.gateway.adhoc.device_controller") as controller:
            tracker.get_device = AsyncMock(return_value=tracker_snapshot)
            controller.execute_command = AsyncMock(side_effect=_wire_to(bench))
            await adhoc.execute_adhoc_command(
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
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, lh = await build_pipetting_bench(recorder)
    runtime = SystemRuntime(
        build.system, event_bus=build.event_bus, labware_store=InMemoryLabwareStore(),
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
async def test_a_pick_the_record_says_is_impossible_reaches_the_operator(
    tip_bench: _Bench,
) -> None:
    """An operator picking from holes the record calls empty is telling us the
    record is wrong, and only a person can say by how much.

    The command has already run, so refusing it is not on offer, and believing
    the operator is right: they were standing at the rack. What cannot happen
    is that it goes quiet. The pick tells us there was a tip at A2; it does not
    tell us what else is on the rack, so the count stays wrong by an amount
    nothing can compute.
    """
    runtime = tip_bench.runtime
    await runtime.labware.set_tip_state(
        tip_bench.labware_id, ["A1"], reason="bench setup", confirm=True,
    )
    settled = await runtime.labware.resolve_contents(tip_bench.labware_id)
    assert settled.provenance is Provenance.KNOWN

    await _adhoc(tip_bench, "pick_up_tips", {
        "picks": [{"tip_rack": tip_bench.labware_name, "positions": ["A2"]}],
    })

    incidents = await runtime.incidents.list(
        category=IncidentCategory.LEDGER_CONTRADICTED,
    )
    assert len(incidents) == 1, (
        "a command that only makes sense if the record is wrong has to reach "
        f"a surface someone reads; found {len(incidents)} incidents"
    )
    detail = incidents[0].detail
    assert isinstance(detail, LedgerContradictionDetail)
    assert detail.labware_name == tip_bench.labware_name
    assert detail.positions == ["A2"]
    assert detail.command == "pick_up_tips"
    assert tip_bench.labware_name in incidents[0].message

    after = await runtime.labware.resolve_contents(tip_bench.labware_id)
    assert after.provenance is Provenance.STALE, (
        "the record is now known to be wrong by an amount only a person can "
        "settle, so it must stop reading as an answer"
    )


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


@pytest_asyncio.fixture
async def arm_bench() -> AsyncIterator[_Bench]:
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, lh = await _build(recorder, wf_name="adhoc_arm")
    runtime = SystemRuntime(
        build.system, event_bus=build.event_bus, labware_store=InMemoryLabwareStore(),
    )
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


def _teachpoint(position_id: str) -> dict[str, Any]:
    return {"teachpoint": {"position_id": position_id}, "external_control": True}


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


@pytest.mark.timeout(30)
async def test_jogging_the_arm_over_an_empty_teachpoint_records_nothing(
    arm_bench: _Bench,
) -> None:
    """Teaching and jogging use the same verbs. Nothing was there to pick."""
    await _adhoc(arm_bench, "pick_at_coords", _teachpoint("stacker"))

    assert arm_bench.ledger_site() == "pad"


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
async def test_a_second_pick_onto_full_jaws_reaches_the_operator(
    arm_bench: _Bench,
) -> None:
    """Closing the jaws on a second plate is a collision the ledger can see.

    The command has already run, so refusing it is not on offer. But the two
    records now contradict each other, and a warning in the log is not a
    surface anyone reads, so it has to land on the incident list.
    """
    second = await _register_at(arm_bench, "stacker")
    await _adhoc(arm_bench, "pick_at_coords", _teachpoint("pad"))
    assert arm_bench.ledger_site() == "arm/gripper"

    await _adhoc(arm_bench, "pick_at_coords", _teachpoint("stacker"))

    conflicts = await arm_bench.runtime.incidents.list(
        category=IncidentCategory.DECK_RECONCILE_CONFLICT,
    )
    assert len(conflicts) == 1, (
        "an operator whose second pick went unrecorded has to be told; "
        f"found {len(conflicts)} incidents"
    )
    incident = conflicts[0]
    assert isinstance(incident.detail, DeckReconcileConflictDetail)
    assert incident.detail.labware_name == second
    assert incident.detail.position_id == "arm/gripper"
    assert incident.detail.blocking_labware_name == arm_bench.labware_name
    assert second in incident.message and arm_bench.labware_name in incident.message
    assert arm_bench.ledger_site() == "arm/gripper", (
        "the record that was already there is not overwritten"
    )
    assert arm_bench.site_of(second) == "stacker/slot", (
        "a refused claim leaves the source alone so the operator can re-issue"
    )


@pytest.mark.timeout(30)
async def test_a_ledger_move_that_fails_halfway_leaves_one_record(
    arm_bench: _Bench,
) -> None:
    """Half a move is worse than none.

    The target is written before the source is cleared. If the clear does not
    happen, the plate reads as present at both positions, which blocks every
    reservation at both and leaves nothing able to say which one is real.
    """
    pad = arm_bench.runtime.system.system_map.resolve_placement_location("pad")

    with patch.object(
        pad, "dispose_labware",
        AsyncMock(side_effect=RuntimeError("the observer wire went down")),
    ):
        await _adhoc(arm_bench, "pick_at_coords", _teachpoint("pad"))

    assert arm_bench.holders_of(arm_bench.labware_name) == ["pad"], (
        "the recording did not go through, so the ledger has to read exactly "
        "as it did before the command"
    )
    assert arm_bench.ledger_site() == "pad"


async def _batched(bench: _Bench, command: str, params: dict[str, Any]) -> None:
    """The same command, sent the way a batch route sends it."""
    tracker_snapshot = MagicMock()
    tracker_snapshot.interfaces = ["ILiquidHandler"]
    token = current_run_mode.set(WorkflowRunMode.LIVE)
    try:
        with patch("orca.gateway.adhoc.device_connection_tracker") as tracker,              patch("orca.gateway.adhoc.device_controller") as controller:
            tracker.get_device = AsyncMock(return_value=tracker_snapshot)
            controller.execute_command = AsyncMock(side_effect=_wire_to(bench))
            executor = BatchExecutor(
                controller,
                lambda _device: WorkflowRunMode.LIVE,
                runtime=bench.runtime,
            )
            result = await executor.execute_batch([DeviceCommand(
                device_id=bench.lh_name, command=command, params=params,
            )])
    finally:
        current_run_mode.reset(token)
    assert result.success, result.results[0].error


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

    await _batched(bench, "move_plate", {
        "plate": bench.labware_name,
        "to_position": WORKING_SITE.split("/", 1)[1],
        "from_position": RESERVOIR_SITE.split("/", 1)[1],
    })

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

    tracker_snapshot = MagicMock()
    tracker_snapshot.interfaces = ["ILiquidHandler"]
    token = current_run_mode.set(WorkflowRunMode.LIVE)
    try:
        with patch("orca.gateway.adhoc.device_connection_tracker") as tracker,              patch("orca.gateway.adhoc.device_controller") as controller:
            tracker.get_device = AsyncMock(return_value=tracker_snapshot)
            controller.execute_command = AsyncMock(side_effect=watching)
            executor = BatchExecutor(
                controller,
                lambda _device: WorkflowRunMode.LIVE,
                runtime=bench.runtime,
            )
            await executor.execute_batch([
                DeviceCommand(device_id=bench.lh_name, command="get_deck_state"),
                DeviceCommand(device_id=bench.lh_name, command="get_deck_state"),
            ])
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

    await record_operator_command(
        runtime.system, bench.lh_name, "move_plate",
        {"plate": name, "to_position": WORKING_SITE.split("/", 1)[1]},
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
    def _picked(request: PickUpTipsRequest) -> list[tuple[list[int] | None, bool]]:
        return [
            (details.use_channels, details.channels_were_counted)
            for _, _, details in _details_for(request)
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
            for _, _, details in _details_for(request)
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
