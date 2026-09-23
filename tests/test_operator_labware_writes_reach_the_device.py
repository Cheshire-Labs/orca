"""An operator's labware write reaches the world an operator writes in.

On the Flex bench, registering a tip rack onto a deck slot left the ledger
holding it at that slot while the instrument's deck had no rack at all. The run
then failed at its pipetting action with "Resource not found" for labware every
hosted surface said was present.

Nothing was dropped: the deck projection ran, against the ambient run mode. A
REST call is never inside an execution, so the ambient mode is the metadata
fallback (PURE_SIM) and the push landed on the device's sim-world driver. The
device verbs already state their base for exactly this reason; the labware
verbs did not.
"""

import pytest

from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.liquid_handler_models import (
    AddDeckLabwareRequest,
    LabwareStateResponse,
    ReconcileDeckOccupancyRequest,
    RemoveDeckLabwareRequest,
)
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.system_runtime import SystemRuntime
from tests.test_labware_state_reconciliation import RESERVOIR_SITE, _build


def _count(recorder: RecordingLiquidHandlerDriver, method: str) -> int:
    return sum(1 for call in recorder.calls if call.method == method)


def _recorder() -> RecordingLiquidHandlerDriver:
    return RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))


async def _runtime(wf_name: str):
    live, sim = _recorder(), _recorder()
    build, _lh = await _build(live, wf_name=wf_name, sim_recorder=sim)
    runtime = SystemRuntime(
        build.system, event_bus=build.event_bus, labware_store=InMemoryLabwareStore(),
    )
    await runtime.start()
    return runtime, build, live, sim


def _unreachable(recorder: RecordingLiquidHandlerDriver) -> None:
    """Make the deck projection fail the way an offline instrument does.

    Every verb that WRITES deck occupancy has to refuse, not just the deck-wide
    one: a projection about a single labware never sends that one. Reads are
    left working, so the projection gets as far as the write that fails.
    """

    async def _refuse_deck(request: ReconcileDeckOccupancyRequest) -> LabwareStateResponse:
        raise ConnectionError("liquid handler unreachable")

    async def _refuse_add(request: AddDeckLabwareRequest) -> None:
        raise ConnectionError("liquid handler unreachable")

    async def _refuse_remove(request: RemoveDeckLabwareRequest) -> None:
        raise ConnectionError("liquid handler unreachable")

    recorder.reconcile_deck_occupancy = _refuse_deck
    recorder.add_deck_labware = _refuse_add
    recorder.remove_deck_labware = _refuse_remove


def _at(build, site: str):
    return build.system.system_map.get_location(site).labware


@pytest.mark.timeout(20)
async def test_registering_labware_on_a_deck_site_reaches_the_live_deck() -> None:
    runtime, _build_, live, sim = await _runtime("operator_register")
    try:
        await runtime.labware.register(
            "reservoir", location=RESERVOIR_SITE, confirm=True,
        )

        assert _count(live, "reconcile_deck_occupancy") == 1
        assert _count(sim, "reconcile_deck_occupancy") == 0
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(20)
async def test_moving_labware_by_hand_reaches_the_live_deck() -> None:
    """Same hole in the edit verb: an operator correcting a location left the
    instrument believing the old one."""
    runtime, _build_, live, sim = await _runtime("operator_edit")
    try:
        snapshot = await runtime.labware.register("reservoir", confirm=True)
        await runtime.labware.edit_location(
            snapshot.id, RESERVOIR_SITE, reason="put it there by hand",
            confirm=True,
        )

        assert _count(live, "reconcile_deck_occupancy") == 1
        assert _count(sim, "reconcile_deck_occupancy") == 0
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(20)
async def test_a_register_the_instrument_refuses_registers_nothing() -> None:
    """Reaching a live instrument means the projection can fail, and a failure
    must not leave the slot claimed for labware the registry never heard of:
    nothing would list it, so nothing could move, edit or discharge it either.
    """
    runtime, build, live, _sim = await _runtime("operator_register_refused")
    try:
        _unreachable(live)

        with pytest.raises(ConnectionError):
            await runtime.labware.register(
                "reservoir", location=RESERVOIR_SITE, confirm=True,
            )

        assert await runtime.labware.list_all() == []
        assert _at(build, RESERVOIR_SITE) is None
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(20)
async def test_an_edit_the_instrument_refuses_leaves_the_labware_where_it_was() -> None:
    """A half-applied edit leaves the plate somewhere neither the operator nor
    the instrument agrees on. The move is refused whole, so the operator's own
    re-issue is the repair."""
    runtime, build, live, _sim = await _runtime("operator_edit_refused")
    try:
        snapshot = await runtime.labware.register(
            "reservoir", location=RESERVOIR_SITE, confirm=True,
        )
        _unreachable(live)

        with pytest.raises(ConnectionError):
            await runtime.labware.edit_location(
                snapshot.id, "lh/carrier-7-0", reason="moved it by hand",
                confirm=True,
            )

        assert _at(build, "lh/carrier-7-0") is None
        assert _at(build, RESERVOIR_SITE) is not None
        moved = await runtime.labware.list_all()
        assert [item.current_location for item in moved] == [RESERVOIR_SITE]
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(20)
async def test_moving_labware_between_two_slots_of_one_deck_is_allowed() -> None:
    """The projection reads the slots, so a target written while the source
    still holds the same labware shows it at two sites. Both holders move
    before anything projects, and the one deck hears about the move once
    rather than twice."""
    runtime, build, live, _sim = await _runtime("operator_edit_same_deck")
    try:
        snapshot = await runtime.labware.register(
            "reservoir", location=RESERVOIR_SITE, confirm=True,
        )
        before = _count(live, "add_deck_labware")
        reconciles = _count(live, "reconcile_deck_occupancy")

        await runtime.labware.edit_location(
            snapshot.id, "lh/carrier-7-0", reason="moved it by hand", confirm=True,
        )

        assert _at(build, RESERVOIR_SITE) is None
        assert _at(build, "lh/carrier-7-0") is not None
        assert _count(live, "add_deck_labware") - before == 1
        assert _count(live, "reconcile_deck_occupancy") == reconciles, (
            "a move of one plate rebuilt the whole deck"
        )
        moved = await runtime.labware.list_all()
        assert [item.current_location for item in moved] == ["lh/carrier-7-0"]
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(20)
async def test_resetting_a_present_labware_to_another_slot_moves_it() -> None:
    """Resetting the history does not put a second copy of the plate down: a
    labware already sitting somewhere leaves that holder as it takes the new one.
    """
    runtime, build, _live, _sim = await _runtime("operator_reset_same_deck")
    try:
        snapshot = await runtime.labware.register(
            "reservoir", location=RESERVOIR_SITE, confirm=True,
        )

        await runtime.labware.reset_location(
            snapshot.id, "lh/carrier-7-0", reason="it starts here now", confirm=True,
        )

        assert _at(build, RESERVOIR_SITE) is None
        assert _at(build, "lh/carrier-7-0") is not None
    finally:
        await runtime.shutdown()
