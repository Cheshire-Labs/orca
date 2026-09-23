"""Registering labware at a slot a thread is waiting on adopts what it waits for.

The bench symptom this closes: `list-labware` showed two rows for every
physical labware, both claiming the same slot.

    r4_plate-e3c8cd14 | flex_1/C2-slot     <- the engine's, never retired
    r4_plate-2fafc0b9 | flex_1/C2-slot     <- the operator's

Registering used to mint a second instance and hand it to the waiting thread,
which then had to be re-pointed in every store that had already cached the
first. The operator is not introducing a second plate -- they are confirming
the one the engine asked for -- so register binds to it instead. Residents
already work this way: reuse always rebinds to the existing instance and
reconciles virtual to physical by position. This applies the same
rule to a manual place.
"""

import asyncio

import pytest

from orca.resource_models.labware_location_service import PlacementState
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.registries import NullGatewayRegistry
from orca.runtime.runtime_interface import SpawnIncompatibleError
from orca.runtime.run_modes import WorkflowRunMode
from orca.workflow_models.status_enums import LabwareThreadStatus
from orca.runtime.system_runtime import SystemRuntime

from tests.test_helpers import create_test_plate_template
from tests.runtime.manual_place_fixtures import (
    _build_manual_place_system,
    _live_connection_source,
)


async def _await_parked_at_manual_place(
    runtime: SystemRuntime, execution_id: str, *, timeout: float = 15.0,
) -> str:
    """Poll until a thread parks for a manual place; return its labware_id."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        for thread in runtime.list_threads(execution_id):
            if (
                thread.status == LabwareThreadStatus.AWAITING_MANUAL_PLACE.name
                and thread.labware_id is not None
            ):
                return thread.labware_id
        await asyncio.sleep(0.05)
    states = [f"{t.name}[{t.status}]" for t in runtime.list_threads(execution_id)]
    raise AssertionError(f"no thread parked for a manual place; threads: {states}")


async def _manual_place_runtime(workflow_name: str) -> tuple[SystemRuntime, InMemoryLabwareStore]:
    system = await _build_manual_place_system(workflow_name)
    store = InMemoryLabwareStore()
    runtime = SystemRuntime(
        system,
        labware_store=store,
        gateway_registry=NullGatewayRegistry(),
        connection_source=_live_connection_source(),
    )
    await runtime.start()
    return runtime, store


async def test_registering_at_an_awaited_slot_returns_the_awaited_labware() -> None:
    runtime, _store = await _manual_place_runtime("wf_adopt_returns")
    try:
        submission = await runtime.submit_workflow(
            "wf_adopt_returns", mode=WorkflowRunMode.LIVE,
        )
        awaited_id = await _await_parked_at_manual_place(runtime, submission.id)

        snap = await runtime.labware.register(
            "plate_wf_adopt_returns", location="pad1", confirm=True,
        )

        assert snap.id == awaited_id, (
            "register minted a second instance instead of binding the one the "
            "thread is holding"
        )
    finally:
        await runtime.shutdown()


async def test_one_physical_labware_lists_once() -> None:
    """The bench symptom directly: one plate, one row, one location."""
    runtime, _store = await _manual_place_runtime("wf_adopt_lists_once")
    try:
        submission = await runtime.submit_workflow(
            "wf_adopt_lists_once", mode=WorkflowRunMode.LIVE,
        )
        await _await_parked_at_manual_place(runtime, submission.id)
        await runtime.labware.register(
            "plate_wf_adopt_lists_once", location="pad1", confirm=True,
        )

        at_pad1 = [
            snap for snap in await runtime.labware.list_all()
            if snap.current_location == "pad1"
        ]
        assert len(at_pad1) == 1, [(s.name, s.current_location) for s in at_pad1]
        assert at_pad1[0].placement is PlacementState.PRESENT
    finally:
        await runtime.shutdown()


async def test_a_barcode_lands_on_the_labware_the_thread_is_holding() -> None:
    """An operator scans the plate they put down; the barcode has to reach the
    instance the run is tracking, not a second one nothing uses."""
    runtime, _store = await _manual_place_runtime("wf_adopt_barcode")
    try:
        submission = await runtime.submit_workflow(
            "wf_adopt_barcode", mode=WorkflowRunMode.LIVE,
        )
        awaited_id = await _await_parked_at_manual_place(runtime, submission.id)

        await runtime.labware.register(
            "plate_wf_adopt_barcode", barcode="BC-4471",
            location="pad1", confirm=True,
        )

        found = await runtime.labware.get_by_barcode("BC-4471")
        assert found.id == awaited_id
    finally:
        await runtime.shutdown()


async def test_an_expected_labware_is_not_persisted_as_a_plate() -> None:
    """A persisted positioned row IS a real physical plate, and boot
    re-places every one of them. Nothing has been placed yet, so a row here
    would resurrect a plate that never existed and block a real slot."""
    runtime, store = await _manual_place_runtime("wf_expected_not_persisted")
    try:
        submission = await runtime.submit_workflow(
            "wf_expected_not_persisted", mode=WorkflowRunMode.LIVE,
        )
        awaited_id = await _await_parked_at_manual_place(runtime, submission.id)
        await runtime.flush_labware_location_writes()

        assert await store.list_active_locations() == [], (
            "an unplaced labware reached the store; a reboot would re-place it"
        )

        await runtime.labware.register(
            "plate_wf_expected_not_persisted", location="pad1", confirm=True,
        )
        await runtime.flush_labware_location_writes()

        assert dict(await store.list_active_locations()) == {awaited_id: "pad1"}
    finally:
        await runtime.shutdown()


async def test_registering_the_wrong_template_is_refused_at_the_register_call() -> None:
    """The operator finds out at their own call, with a slot and a template to
    act on, instead of the thread failing behind them."""
    runtime, _store = await _manual_place_runtime("wf_adopt_wrong_template")
    # A second template the system really knows, so the refusal is about the
    # mismatch and not about an unknown name.
    runtime.system.add_labware_template(create_test_plate_template("some_other_plate"))
    try:
        submission = await runtime.submit_workflow(
            "wf_adopt_wrong_template", mode=WorkflowRunMode.LIVE,
        )
        await _await_parked_at_manual_place(runtime, submission.id)

        with pytest.raises(SpawnIncompatibleError) as exc_info:
            await runtime.labware.register(
                "some_other_plate", location="pad1", confirm=True,
            )
        assert exc_info.value.expected_template == "plate_wf_adopt_wrong_template"
        assert exc_info.value.actual_template == "some_other_plate"

        pad1 = runtime.system.system_map.get_location("pad1")
        assert pad1.labware is None, "a refused register must write nothing"
        threads = runtime.list_threads(submission.id)
        assert any(
            t.status == LabwareThreadStatus.AWAITING_MANUAL_PLACE.name for t in threads
        ), "the thread must still be waiting for the labware it asked for"
    finally:
        await runtime.shutdown()


async def test_registering_where_nothing_waits_still_mints_a_fresh_instance() -> None:
    """Adoption is not a takeover: an operator stocking an unwatched slot gets
    new labware exactly as before."""
    runtime, _store = await _manual_place_runtime("wf_adopt_unwatched")
    try:
        snap = await runtime.labware.register(
            "plate_wf_adopt_unwatched", location="pad1", confirm=True,
        )
        assert snap.placement is PlacementState.PRESENT
        listed = [s.id for s in await runtime.labware.list_all()]
        assert snap.id in listed
    finally:
        await runtime.shutdown()


async def test_the_thread_stays_reachable_by_the_id_operators_are_given() -> None:
    """`ExecutingThreadRegistry` keys its dict by the thread id at CREATE time,
    and `System.pause_thread` / `resume_thread` look up through it -- which is
    what `mutate_on_next_pause` is built on, so insert_method, replace_method,
    skip_action and insert_action all ran through that key.

    While a LIVE manual place could change a thread's id, every one of those
    verbs raised `Thread <id> has not been created yet` on a manual-place
    thread, because the id the operator is handed was never a key. The id no
    longer moves, so the lookup holds.
    """
    runtime, _store = await _manual_place_runtime("wf_thread_stays_reachable")
    try:
        submission = await runtime.submit_workflow(
            "wf_thread_stays_reachable", mode=WorkflowRunMode.LIVE,
        )
        thread_id = await _await_parked_at_manual_place(runtime, submission.id)
        await runtime.labware.register(
            "plate_wf_thread_stays_reachable", location="pad1", confirm=True,
        )

        # The id every operator surface reports for this thread.
        reported = [
            t.id for t in runtime.list_threads(submission.id)
            if t.labware_id == thread_id
        ]
        assert reported == [thread_id]
        assert runtime.system.get_executing_thread(thread_id).id == thread_id
    finally:
        await runtime.shutdown()


async def test_asserting_an_expected_labware_is_where_it_belongs_really_places_it() -> None:
    """`edit_location` short-circuits when source == target, on the reasoning
    that nothing moved. An EXPECTED labware has no source, so that reasoning
    does not hold: the operator is telling us the plate is now on the pad, and
    short-circuiting would write a store position for a plate nothing had put
    down -- the exact lie this work removed.
    """
    runtime, store = await _manual_place_runtime("wf_edit_to_expected")
    try:
        submission = await runtime.submit_workflow(
            "wf_edit_to_expected", mode=WorkflowRunMode.LIVE,
        )
        awaited_id = await _await_parked_at_manual_place(runtime, submission.id)

        await runtime.labware.edit_location(
            awaited_id, "pad1", reason="operator put it down", confirm=True,
        )
        await runtime.flush_labware_location_writes()

        pad1 = runtime.system.system_map.get_location("pad1")
        assert pad1.labware is not None, "the slot must actually be written"
        assert pad1.labware.id == awaited_id
        snap = await runtime.labware.get_by_id(awaited_id)
        assert snap.placement is PlacementState.PRESENT
        assert dict(await store.list_active_locations()) == {awaited_id: "pad1"}
    finally:
        await runtime.shutdown()
