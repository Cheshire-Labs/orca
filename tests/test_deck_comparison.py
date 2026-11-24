"""Somebody has to be able to ask whether the ledger and the deck agree.

The reconcile is a one-way push: it builds the driver's deck from the ledger and
overwrites whatever was there. So the one moment the driver's own answer exists
is before that push, and until now nothing looked at it. An operator who drove
the robot at the instrument, or whose gripper move died mid-flight, had no verb
that would tell them the two models had parted company.
"""

from typing import AsyncIterator

import pytest
import pytest_asyncio

from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.liquid_handler_models import (
    DeckResourceState,
    DeckStateResponse,
    GetDeckStateRequest,
    InterruptedMove,
    MovePlateRequest,
    ResetDeckLabwareRequest,
)
from orca.operations.device import CompareDeckOperation
from orca.operations.device_models import CompareDeckRequest
from orca.runtime.incident_store import IncidentCategory
from orca.runtime.system_runtime import SystemRuntime
from orca.system.system_interface import DeckConflictReason
from tests.test_labware_state_reconciliation import RESERVOIR_SITE, _build
from tests.memory_activity import memory_runtime
from tests.recording_handler import RecordingHandler

WORKING_SITE = "carrier-7-0"


class _Bench:
    def __init__(self, runtime: SystemRuntime, lh_name: str, labware_name: str) -> None:
        self.runtime = runtime
        self.lh_name = lh_name
        self.labware_name = labware_name

    @property
    def driver(self) -> RecordingLiquidHandlerDriver:
        return self.runtime.system.get_device(self.lh_name).driver

    async def drive_the_robot_by_hand(self, to_site: str) -> None:
        """Move a plate the way the instrument's own touchscreen would.

        Straight at the driver, past every orca surface, which is exactly what
        an operator standing at the robot does.
        """
        await self.driver.move_plate(MovePlateRequest(
            plate=self.labware_name,
            to_position=to_site,
            from_position=RESERVOIR_SITE.split("/", 1)[1],
        ))


@pytest_asyncio.fixture
async def bench() -> AsyncIterator[_Bench]:
    recorder = RecordingHandler()
    build, lh = await _build(recorder, wf_name="deck_comparison")
    runtime = await memory_runtime(
        build.system, event_bus=build.event_bus
    )
    await runtime.start()
    snapshot = await runtime.labware.register(
        "reservoir", location=RESERVOIR_SITE, confirm=True,
    )
    name = next(
        lw.name for lw in await runtime.labware.list_all() if lw.id == snapshot.id
    )
    try:
        yield _Bench(runtime, lh.name, name)
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(30)
async def test_agreement_is_reported_as_agreement(bench: _Bench) -> None:
    comparison = await bench.runtime.devices.compare_deck(bench.lh_name)

    assert comparison.disagreements == ()
    assert comparison.interrupted_move_labware is None


@pytest.mark.timeout(30)
async def test_a_plate_moved_at_the_instrument_shows_up_as_a_disagreement(
    bench: _Bench,
) -> None:
    await bench.drive_the_robot_by_hand(WORKING_SITE)

    comparison = await bench.runtime.devices.compare_deck(bench.lh_name)

    assert len(comparison.disagreements) == 1, (
        f"one plate is in two places; got {comparison.disagreements}"
    )
    conflict = comparison.disagreements[0]
    assert conflict.labware_name == bench.labware_name
    assert conflict.reason is DeckConflictReason.DRIVER_SITE_DIFFERS
    assert conflict.position_id == RESERVOIR_SITE, "the ledger's side"
    assert conflict.driver_site == WORKING_SITE, "the driver's side"


@pytest.mark.timeout(30)
async def test_a_disagreement_reaches_the_incident_surface(bench: _Bench) -> None:
    """Nobody has to be looking at the answer for the conflict to be recorded.

    The reconcile is what files. A bare compare is read-only on both sides,
    because an assistant is told to run it routinely and one queue entry per
    check per conflict would bury the ones somebody has to act on.
    """
    await bench.drive_the_robot_by_hand(WORKING_SITE)

    await bench.runtime.devices.reconcile_deck(bench.lh_name, confirm=True)

    conflicts = await bench.runtime.incidents.list(
        category=IncidentCategory.DECK_RECONCILE_CONFLICT,
    )
    assert len(conflicts) == 1, f"got {[i.message for i in conflicts]}"
    assert WORKING_SITE in conflicts[0].message
    assert RESERVOIR_SITE in conflicts[0].message


@pytest.mark.timeout(30)
async def test_reconcile_hands_back_what_it_is_about_to_overwrite(
    bench: _Bench,
) -> None:
    """The push destroys the evidence, so the comparison has to precede it."""
    await bench.drive_the_robot_by_hand(WORKING_SITE)

    comparison = await bench.runtime.devices.reconcile_deck(
        bench.lh_name, confirm=True,
    )

    assert comparison is not None
    assert [c.reason for c in comparison.disagreements] == [
        DeckConflictReason.DRIVER_SITE_DIFFERS
    ]
    after = await bench.runtime.devices.compare_deck(bench.lh_name)
    assert after.disagreements == (), (
        "the reconcile pushed the ledger onto the driver, so they agree now"
    )


@pytest.mark.timeout(30)
async def test_a_rebuilt_session_is_not_a_pile_of_conflicts(bench: _Bench) -> None:
    """An empty driver deck is a deck waiting to be re-declared, not a dispute.

    Every reconnect and every lifecycle verb hits this. Reporting one conflict
    per resident each time would bury the disagreements that mean something.
    """
    await bench.driver.reset_deck_labware(ResetDeckLabwareRequest())

    comparison = await bench.runtime.devices.compare_deck(bench.lh_name)

    assert comparison.driver_deck_empty
    conflicts = await bench.runtime.incidents.list(
        category=IncidentCategory.DECK_RECONCILE_CONFLICT,
    )
    assert not conflicts, (
        "an empty deck raised conflicts an operator would have to wade through"
    )


@pytest.mark.timeout(30)
async def test_an_empty_driver_deck_does_not_agree_with_a_record_that_has_labware_there(bench: _Bench) -> None:
    """The record has a reservoir on the deck and the driver reports none: the record has not reached the driver."""
    await bench.driver.reset_deck_labware(ResetDeckLabwareRequest())

    answer = await CompareDeckOperation(bench.runtime).run(CompareDeckRequest(device_id=bench.lh_name))

    assert answer.driver_deck_empty is True
    assert answer.agrees is False
    assert answer.disagreements == [], "an empty driver deck is still not a list of plates to settle one by one"


@pytest.mark.timeout(30)
async def test_an_empty_driver_deck_agrees_with_a_record_that_has_nothing_there(bench: _Bench) -> None:
    labware_id = bench.runtime.system.get_labware(bench.labware_name).id
    await bench.runtime.labware.discharge_labware(labware_id, confirm=True)

    answer = await CompareDeckOperation(bench.runtime).run(CompareDeckRequest(device_id=bench.lh_name))

    assert answer.driver_deck_empty is True
    assert answer.agrees is True


@pytest.mark.timeout(30)
async def test_a_bare_compare_files_nothing(bench: _Bench) -> None:
    """Read-only means the incident queue is untouched too.

    An assistant is told to compare after anything touched the robot, so a
    compare that filed would put a fresh copy of every conflict on the queue
    each time it looked.
    """
    await bench.drive_the_robot_by_hand(WORKING_SITE)

    comparison = await bench.runtime.devices.compare_deck(bench.lh_name)

    assert comparison.disagreements, "the compare should still REPORT the conflict"
    conflicts = await bench.runtime.incidents.list(
        category=IncidentCategory.DECK_RECONCILE_CONFLICT,
    )
    assert conflicts == [], f"a read-only compare filed {len(conflicts)} incident(s)"


@pytest.mark.timeout(30)
async def test_deck_furniture_is_not_a_disagreement(bench: _Bench) -> None:
    """A Flex's trash sits on the deck and will never be in the ledger.

    `get_deck_state().labware` is an occupancy list, so it carries the fixtures
    the machine was built with. Counting those as labware nobody declared made
    every compare on such a machine answer "we disagree" about furniture.
    """
    driver = bench.driver
    real_get_deck_state = driver.get_deck_state

    async def with_furniture(request: GetDeckStateRequest) -> DeckStateResponse:
        state = await real_get_deck_state(request)
        return state.model_copy(update={"labware": [
            *state.labware,
            DeckResourceState(
                name="trash_container", type="Trash",
                category="trash", site="A3-slot",
            ),
            DeckResourceState(
                name="carrier_3", type="PlateCarrier",
                category="plate_carrier", site="carrier-3-0",
            ),
        ]})

    driver.get_deck_state = with_furniture  # type: ignore[method-assign]

    comparison = await bench.runtime.devices.compare_deck(bench.lh_name)

    assert comparison is not None
    unknown = [
        d.labware_name for d in comparison.disagreements
        if d.reason is DeckConflictReason.UNKNOWN_TO_LEDGER
    ]
    assert unknown == [], f"furniture reported as a conflict: {unknown}"


async def _strand_a_stand_in(bench: _Bench) -> None:
    """Make the driver report a gripper move that died with no labware named."""
    driver = bench.driver
    real_get_deck_state = driver.get_deck_state

    async def with_a_stranded_stand_in(request: GetDeckStateRequest) -> DeckStateResponse:
        state = await real_get_deck_state(request)
        return state.model_copy(update={"interrupted_move": InterruptedMove(
            from_site="carrier-7-1", to_site="carrier-7-2", error="RuntimeError: Run was cancelled",
        )})

    driver.get_deck_state = with_a_stranded_stand_in  # type: ignore[method-assign]


@pytest.mark.timeout(30)
async def test_an_interrupted_move_of_nothing_on_record_is_a_disagreement_that_names_no_labware(bench: _Bench) -> None:
    """The gripper may hold a plate nobody recorded, so the two sides must not read as agreeing."""
    await _strand_a_stand_in(bench)

    comparison = await bench.runtime.devices.compare_deck(bench.lh_name)

    [entry] = comparison.disagreements
    assert entry.reason is DeckConflictReason.INTERRUPTED_MOVE
    assert (entry.labware_id, entry.labware_name) == (None, None)
    assert entry.position_id.endswith("/carrier-7-1") and entry.driver_site == "carrier-7-2"
    assert entry.detail is not None and "Look at the gripper" in entry.detail
    assert comparison.interrupted_move_labware is None


@pytest.mark.timeout(30)
async def test_compare_deck_does_not_agree_while_a_move_of_nothing_on_record_is_interrupted(bench: _Bench) -> None:
    await _strand_a_stand_in(bench)

    answer = await CompareDeckOperation(bench.runtime).run(CompareDeckRequest(device_id=bench.lh_name))

    assert answer.agrees is False
    [entry] = answer.disagreements
    assert entry.labware_name is None and entry.labware_id is None
    assert entry.detail is not None and "Look at the gripper" in entry.detail


@pytest.mark.timeout(30)
async def test_a_reconcile_files_an_incident_that_tells_the_operator_to_look_at_the_gripper(bench: _Bench) -> None:
    await _strand_a_stand_in(bench)

    await bench.runtime.devices.reconcile_deck(bench.lh_name, confirm=True)

    [incident] = await bench.runtime.incidents.list(category=IncidentCategory.DECK_RECONCILE_CONFLICT)
    assert "Look at the gripper" in incident.message
    assert "None" not in incident.message
