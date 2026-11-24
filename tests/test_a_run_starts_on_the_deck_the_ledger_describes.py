"""A system is one set of labware: a run starts on the deck the ledger describes, and so does any command to a device
whose world may not match the record."""

import asyncio

from unittest.mock import AsyncMock, MagicMock

import pytest
from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.labware_models import LabwareIdentity
from cheshire_drivers.sims import SimTransporterDriver
from cheshire_drivers.liquid_handler_models import DeckResourceConfig, ReconcileDeckOccupancyRequest

from tests.gateway._operator_command import run_operator_command
from orca.devices.devices import LiquidHandler
from orca.resource_models.transporter import Transporter
from orca.modes.run_modes import WorkflowRunMode, mode_scope
from orca.runtime.system_runtime import SystemRuntime
from tests.test_a_discharge_clears_every_deck_world import _build, _deck_names
from tests.gateway_parts import gateway_with
from tests.memory_activity import memory_runtime
from tests.test_helpers import run_to_quiescence
from tests.recording_handler import RecordingHandler
from tests.real_device_path import the_real_device_path

WORKFLOW = "resident_reagent_wf"
OPERATOR_PLATE_TYPE = "AGenBio_1_troughplate_190000uL_Fl"
FREE_SITE = "lh/carrier-25-3"


async def _runtime() -> tuple[SystemRuntime, RecordingLiquidHandlerDriver]:
    """A runtime whose liquid handler keeps one deck for every mode, which the test reads."""
    driver = RecordingHandler()
    build, _ = await _build(driver, driver)
    runtime = await memory_runtime(
        build.system, event_bus=build.event_bus
    )
    await runtime.start()
    return runtime, driver


async def _leave_a_stale_rack(runtime: SystemRuntime, driver: RecordingLiquidHandlerDriver) -> None:
    """A run, then a clear that misses this deck, leaving a rack the ledger does not hold."""
    await _run(runtime)
    await runtime.labware.clear_all_labware(force=True, confirm=True)
    await _put_a_rack_on_by_hand(driver)


async def _put_a_rack_on_by_hand(driver: RecordingLiquidHandlerDriver) -> None:
    """The driver's deck changed outside Orca: nothing marks its world."""
    await driver.reconcile_deck_occupancy(ReconcileDeckOccupancyRequest(resources=[
        DeckResourceConfig(name="stale_rack", catalog_ref=OPERATOR_PLATE_TYPE, parent_id="carrier-25", site_index=3),
    ]))
    assert "stale_rack" in await _deck_names(driver)


async def _run(runtime: SystemRuntime) -> None:
    await run_to_quiescence(runtime, (await runtime.submit_workflow(WORKFLOW, mode=WorkflowRunMode.SIM)).id)


@pytest.mark.slow
@pytest.mark.asyncio
async def test_labware_the_ledger_no_longer_holds_is_gone_from_the_deck_when_the_next_run_starts() -> None:
    runtime, driver = await _runtime()
    try:
        await _leave_a_stale_rack(runtime, driver)

        await _run(runtime)

        assert "stale_rack" not in await _deck_names(driver)
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_a_deck_changed_by_hand_with_nothing_marked_is_rebuilt_when_the_next_run_starts() -> None:
    """Nothing in Orca saw the change, so only the run's own start can catch it."""
    runtime, driver = await _runtime()
    try:
        await _leave_a_stale_rack(runtime, driver)
        for mode in (WorkflowRunMode.SIM, WorkflowRunMode.LIVE):
            with mode_scope(mode):
                await runtime.system.device_worlds.bring_up_to_date("lh")
        await _put_a_rack_on_by_hand(driver)

        await _run(runtime)

        assert "stale_rack" not in await _deck_names(driver)
    finally:
        await runtime.shutdown()


async def _send_get_deck_state(runtime: SystemRuntime) -> None:
    tracker, controller = MagicMock(), MagicMock()
    tracker.get_device = AsyncMock(return_value=MagicMock(interfaces=["ILiquidHandler"]))
    controller.execute_command = AsyncMock(return_value=None)
    await run_operator_command(
        gateway_with(tracker=tracker, controller=controller), runtime, "lh", "get_deck_state", {},
    )


@pytest.mark.out_of_run
@pytest.mark.slow
@pytest.mark.asyncio
async def test_an_operator_command_to_a_device_whose_world_is_marked_starts_on_the_deck_the_ledger_describes() -> None:
    runtime, driver = await _runtime()
    try:
        await _leave_a_stale_rack(runtime, driver)
        runtime.system.device_worlds.mark_every_world("lh")

        await _send_get_deck_state(runtime)

        assert "stale_rack" not in await _deck_names(driver)
    finally:
        await runtime.shutdown()


@pytest.mark.out_of_run
@pytest.mark.slow
@pytest.mark.asyncio
async def test_an_operator_command_to_a_world_nothing_marked_rebuilds_nothing() -> None:
    """The deck is pushed only when it may disagree with the record, not before every command."""
    runtime, driver = await _runtime()
    try:
        await _leave_a_stale_rack(runtime, driver)
        # The sim run's commands marked the live world; this command rebuilds it.
        await _send_get_deck_state(runtime)
        await _put_a_rack_on_by_hand(driver)

        await _send_get_deck_state(runtime)

        assert "stale_rack" in await _deck_names(driver)
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_two_runs_submitted_together_still_start_on_the_ledgers_deck() -> None:
    """Each run sees the other's unfinished task; one of them must still rebuild before either moves labware."""
    runtime, driver = await _runtime()
    try:
        await _leave_a_stale_rack(runtime, driver)
        both_registered = asyncio.Event()
        arrived = 0
        bring_up = runtime.system.ensure_runtime_initialized

        async def hold_until_both_are_registered() -> None:
            nonlocal arrived
            await bring_up()
            arrived += 1
            if arrived == 2:
                both_registered.set()
            await both_registered.wait()

        setattr(runtime.system, "ensure_runtime_initialized", hold_until_both_are_registered)
        first = await runtime.submit_workflow(WORKFLOW, mode=WorkflowRunMode.SIM)
        second = await runtime.submit_workflow(WORKFLOW, mode=WorkflowRunMode.SIM)
        await run_to_quiescence(runtime, first.id)
        await run_to_quiescence(runtime, second.id)

        assert "stale_rack" not in await _deck_names(driver)
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_a_command_rebuilds_only_the_device_it_is_sent_to() -> None:
    """A handler whose lab PC is off must not fail a command to the arm, so the arm's command leaves the deck alone."""
    runtime, driver = await _runtime()
    try:
        await _leave_a_stale_rack(runtime, driver)
        runtime.system.device_worlds.mark_everything()

        await runtime.system.device_worlds.bring_up_to_date("arm")

        assert "stale_rack" in await _deck_names(driver)
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_a_command_does_not_wait_behind_a_busy_device() -> None:
    """A busy device refuses the command itself; the rebuild before it must not queue behind the lock."""
    runtime, driver = await _runtime()
    try:
        await _leave_a_stale_rack(runtime, driver)
        runtime.system.device_worlds.mark_every_world("lh")
        handler = runtime.system.get_device("lh")
        assert isinstance(handler, LiquidHandler)
        held, release = asyncio.Event(), asyncio.Event()

        async def a_long_aspirate() -> None:
            async with handler.lock.held_for("a long aspirate"):
                held.set()
                await release.wait()

        aspirate = asyncio.create_task(a_long_aspirate())
        await held.wait()
        try:
            await asyncio.wait_for(runtime.system.device_worlds.bring_up_to_date("lh"), timeout=5.0)
        finally:
            release.set()
            await aspirate

        assert "stale_rack" in await _deck_names(driver)
        assert await runtime.system.device_worlds.owes_a_rebuild(handler), "the skipped rebuild is still owed"
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_a_run_starts_with_the_arm_seeing_only_what_the_ledger_holds() -> None:
    """An arm's simulator keeps its own record of plates at its positions; a stale one would make a pick fail."""
    runtime, _driver = await _runtime()
    try:
        arm = runtime.system.get_resource("arm")
        assert isinstance(arm, Transporter)
        position = (await arm.get_teachpoints())[0].position_id
        stale = LabwareIdentity(labware_id="stale-plate", labware_type="Plate_96")
        simulator = the_real_device_path().simulator("arm")
        assert isinstance(simulator, SimTransporterDriver)
        simulator._seed_resource(position, simulator._resource_for_identity(stale))
        assert simulator._resource_at(position) is not None

        await _run(runtime)

        assert simulator._resource_at(position) is None
    finally:
        await runtime.shutdown()
