"""A thread that names its partner plate by barcode is served by that plate and no other."""
import asyncio
import logging

import pytest

from orca.modes.run_modes import WorkflowRunMode
from orca.workflow_models.status_enums import RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext

from orca.resource_models.capacity import CapacityPolicy, OverflowAction
from orca.resource_models.labware import LabwareInstance
from orca.state.records import DeclaredTracking
from orca.runtime.status_models import ThreadSnapshot
from orca.workflow_models.group_aware_labware_registry import _decompose_slot_scope
from orca.workflow_models.labware_group import (
    AcquisitionValidationError, BarcodeAcquisition, LabwareGroup, LabwareGroupMember,
)
from orca.workflow_models.labware_state import named_partner_slot_key
from orca.workflow_models.submission import BatchMode

from tests.partner_pairing_world import TERMINAL_STATUSES, PairingWorld, build_pairing_world, sample_group
from tests.test_helpers import wait_until


async def test_two_sample_plates_are_each_tagged_with_the_index_plate_they_name() -> None:
    world = await build_pairing_world({"S-1": "IDX-1", "S-2": "IDX-2"})
    for barcode in ("S-1", "S-2"):
        await world.register("sample", barcode)
    for barcode in ("IDX-1", "IDX-2"):
        await world.register("index", barcode)
    await world.runtime.start()
    try:
        statuses = await world.run_to_the_end([sample_group("g1", "S-1"), sample_group("g2", "S-2")])
    finally:
        await world.runtime.shutdown()

    assert sorted(world.pairings.seen, key=repr) == [("S-1", "IDX-1"), ("S-2", "IDX-2")]
    assert all(status == "COMPLETED" for status in statuses.values()), statuses


async def test_three_pairs_registered_in_reverse_order_are_each_tagged_with_their_own() -> None:
    world = await build_pairing_world({"S-1": "IDX-1", "S-2": "IDX-2", "S-3": "IDX-3"})
    for barcode in ("IDX-3", "IDX-2", "IDX-1"):
        await world.register("index", barcode)
    for barcode in ("S-3", "S-2", "S-1"):
        await world.register("sample", barcode)
    await world.runtime.start()
    try:
        statuses = await world.run_to_the_end(
            [sample_group("g3", "S-3"), sample_group("g1", "S-1"), sample_group("g2", "S-2")],
        )
    finally:
        await world.runtime.shutdown()

    assert sorted(world.pairings.seen, key=repr) == [("S-1", "IDX-1"), ("S-2", "IDX-2"), ("S-3", "IDX-3")]
    assert all(status == "COMPLETED" for status in statuses.values()), statuses


async def test_a_barcode_nobody_holds_pauses_the_sample_and_a_retry_after_registering_it_finishes() -> None:
    world = await build_pairing_world({"S-1": "IDX-1"})
    await world.register("sample", "S-1")
    await world.runtime.start()
    try:
        execution_id = await world.submit([sample_group("g1", "S-1")])
        paused = await world.wait_for_a_pause(execution_id, "sample")

        assert "IDX-1" in (paused.last_error or "") and "no labware with that barcode" in (paused.last_error or "")
        assert world.pairings.seen == []
        assert not [name for name in world.threads(execution_id) if name.startswith("index")]

        await world.register("index", "IDX-1")
        world.runtime.recover_thread(execution_id, paused.id, RecoveryDecision.RETRY)
        statuses = await world.wait_for_the_end(execution_id)
    finally:
        await world.runtime.shutdown()

    assert world.pairings.seen == [("S-1", "IDX-1")]
    assert all(status == "COMPLETED" for status in statuses.values()), statuses


async def test_a_barcode_held_by_another_kind_of_labware_is_refused() -> None:
    world = await build_pairing_world({"S-1": "S-2"})
    await world.register("sample", "S-1")
    await world.register("sample", "S-2")
    await world.runtime.start()
    try:
        execution_id = await world.submit([sample_group("g1", "S-1")])
        paused = await world.wait_for_a_pause(execution_id, "sample")
    finally:
        await world.runtime.shutdown()

    assert "which is a 'sample'" in (paused.last_error or "")
    assert world.pairings.seen == []


async def test_a_second_group_naming_a_plate_in_use_waits_and_is_served_by_it_after_a_retry() -> None:
    world = await build_pairing_world({"S-1": "IDX-1", "S-2": "IDX-1"}, index_stays_at_waste=True)
    for barcode in ("S-1", "S-2"):
        await world.register("sample", barcode)
    await world.register("index", "IDX-1")
    await world.runtime.start()
    try:
        execution_id = await world.submit([sample_group("g1", "S-1"), sample_group("g2", "S-2")])
        paused = await world.wait_for_a_pause(execution_id, "sample")
        assert "is in use by thread" in (paused.last_error or "")

        def the_first_pair_is_done() -> bool:
            return len(world.pairings.seen) == 1 and all(
                thread.status == "COMPLETED"
                for name, thread in world.threads(execution_id).items() if name.startswith("index")
            )

        await wait_until(the_first_pair_is_done, timeout=90.0, message="the first pair never finished")
        world.runtime.recover_thread(execution_id, paused.id, RecoveryDecision.RETRY)
        statuses = await world.wait_for_the_end(execution_id)
    finally:
        await world.runtime.shutdown()

    assert sorted(world.pairings.seen, key=repr) == [("S-1", "IDX-1"), ("S-2", "IDX-1")]
    assert all(status == "COMPLETED" for status in statuses.values()), statuses


async def test_a_step_does_not_run_when_a_contributor_named_a_different_partner() -> None:
    world = await build_pairing_world(
        {"S-1": "IDX-1"},
        index_thread_first=lambda ctx: ctx.set_partner_constraint("sample", {"barcode": "S-9"}),
    )
    await world.register("sample", "S-1")
    await world.register("index", "IDX-1")
    await world.runtime.start()
    try:
        execution_id = await world.submit([sample_group("g1", "S-1")])
        paused = await world.wait_for_a_pause(execution_id, "sample")
    finally:
        await world.runtime.shutdown()

    assert "needs the 'sample' with barcode 'S-9'" in (paused.last_error or "")
    assert world.pairings.seen == []


async def test_a_thread_with_no_constraint_never_gets_a_plate_another_thread_named() -> None:
    world = await build_pairing_world({"S-1": "IDX-1"})
    for barcode in ("S-1", "S-2"):
        await world.register("sample", barcode)
    await world.register("index", "IDX-1")
    await world.runtime.start()
    try:
        statuses = await world.run_to_the_end([sample_group("g1", "S-1"), sample_group("g2", "S-2")])
    finally:
        await world.runtime.shutdown()

    assert sorted(world.pairings.seen, key=repr) == [("S-1", "IDX-1"), ("S-2", None)]
    assert all(status == "COMPLETED" for status in statuses.values()), statuses


async def test_a_named_plate_whose_thread_ended_serves_the_next_step_on_a_new_thread() -> None:
    world = await build_pairing_world(
        {"S-1": "IDX-1"}, tags_per_sample=2, index_serves_one_tag_then_goes_home=True,
    )
    await world.register("sample", "S-1")
    await world.register("index", "IDX-1")
    await world.runtime.start()
    try:
        statuses = await world.run_to_the_end([sample_group("g1", "S-1")])
    finally:
        await world.runtime.shutdown()

    assert world.pairings.seen == [("S-1", "IDX-1"), ("S-1", "IDX-1")]
    assert all(status == "COMPLETED" for status in statuses.values()), statuses


async def test_a_named_plate_standing_in_a_hotel_is_taken_from_there() -> None:
    world = await build_pairing_world({"S-1": "IDX-1"})
    await world.register("sample", "S-1")
    index = await world.register("index", "IDX-1")
    await world.runtime.start()
    try:
        await world.stand(index, "hotel")
        statuses = await world.run_to_the_end([sample_group("g1", "S-1")])
    finally:
        await world.runtime.shutdown()

    assert world.pairings.seen == [("S-1", "IDX-1")]
    assert all(status == "COMPLETED" for status in statuses.values()), statuses
    assert world.where(index)[0] == "hotel" and "index_pad" not in world.where(index)


async def _paused_sample(world: PairingWorld, groups: list[LabwareGroup]) -> tuple[str, ThreadSnapshot]:
    execution_id = await world.submit(groups)
    return execution_id, await world.wait_for_a_pause(execution_id, "sample")


def _threads_named(world: PairingWorld, execution_id: str, prefix: str) -> list[str]:
    return [name for name in world.threads(execution_id) if name.startswith(prefix)]


async def test_a_resident_partner_with_the_named_barcode_serves() -> None:
    world = await build_pairing_world({"S-1": "IDX-1"}, index_is_a_resident=True)
    await world.register("sample", "S-1")
    resident = await world.register("index", "IDX-1")
    await world.runtime.start()
    try:
        await world.stand(resident, "index_pad")
        statuses = await world.run_to_the_end([sample_group("g1", "S-1")])
    finally:
        await world.runtime.shutdown()

    assert world.pairings.seen == [("S-1", "IDX-1")]
    assert all(status == "COMPLETED" for status in statuses.values()), statuses


async def test_a_resident_partner_with_another_barcode_is_refused() -> None:
    world = await build_pairing_world({"S-1": "IDX-1"}, index_is_a_resident=True)
    await world.register("sample", "S-1")
    resident = await world.register("index", "IDX-9")
    await world.runtime.start()
    try:
        await world.stand(resident, "index_pad")
        _, paused = await _paused_sample(world, [sample_group("g1", "S-1")])
    finally:
        await world.runtime.shutdown()

    assert "barcode 'IDX-9'" in (paused.last_error or "") and "named the 'index' with barcode 'IDX-1'" in (paused.last_error or "")
    assert world.pairings.seen == []


async def test_a_named_plate_that_is_used_up_is_refused_and_no_other_plate_is_made() -> None:
    async def used_up(labware: LabwareInstance, demand: DeclaredTracking | None) -> bool:
        return False

    world = await build_pairing_world({"S-1": "IDX-1"}, index_can_continue=used_up)
    await world.register("sample", "S-1")
    await world.register("index", "IDX-1")
    await world.runtime.start()
    try:
        execution_id, paused = await _paused_sample(world, [sample_group("g1", "S-1")])
        index_threads = _threads_named(world, execution_id, "index")
    finally:
        await world.runtime.shutdown()

    assert "used up" in (paused.last_error or "")
    assert world.pairings.seen == [] and index_threads == []


async def test_a_named_plate_whose_thread_is_full_serves_the_next_step_after_a_retry() -> None:
    world = await build_pairing_world(
        {"S-1": "IDX-1"},
        tags_per_sample=2,
        index_capacity=CapacityPolicy(max_contributions=1, overflow_action=OverflowAction.NEW), index_stays_at_waste=True)
    await world.register("sample", "S-1")
    await world.register("index", "IDX-1")
    await world.runtime.start()
    try:
        execution_id, paused = await _paused_sample(world, [sample_group("g1", "S-1")])
        assert "has taken all the work it accepts" in (paused.last_error or "")
        await wait_until(
            lambda: all(
                thread.status == "COMPLETED"
                for name, thread in world.threads(execution_id).items() if name.startswith("index")
            ),
            timeout=90.0, message="the full index thread never ended",
        )
        world.runtime.recover_thread(execution_id, paused.id, RecoveryDecision.RETRY)
        statuses = await world.wait_for_the_end(execution_id)
    finally:
        await world.runtime.shutdown()

    assert world.pairings.seen == [("S-1", "IDX-1"), ("S-1", "IDX-1")]
    assert all(status == "COMPLETED" for status in statuses.values()), statuses


async def test_a_refused_named_plate_binds_no_other_input_and_an_abort_leaves_nothing_waiting() -> None:
    world = await build_pairing_world({"S-1": "IDX-1"}, adapter_for_sample={})
    await world.register("sample", "S-1")
    await world.runtime.start()
    try:
        execution_id, paused = await _paused_sample(world, [sample_group("g1", "S-1")])
        bound = _threads_named(world, execution_id, "adapter") + _threads_named(world, execution_id, "index")
        world.runtime.recover_thread(execution_id, paused.id, RecoveryDecision.ABORT_THREAD)
        statuses = await world.wait_for_the_end(execution_id)
    finally:
        await world.runtime.shutdown()

    assert bound == []
    assert world.pairings.seen == []
    assert [name for name in statuses if not name.startswith("sample")] == []


async def test_a_thread_can_name_two_partners_of_different_kinds() -> None:
    world = await build_pairing_world({"S-1": "IDX-1"}, adapter_for_sample={"S-1": "AD-1"})
    await world.register("sample", "S-1")
    await world.register("index", "IDX-1")
    await world.register("adapter", "AD-2")
    await world.register("adapter", "AD-1")
    await world.runtime.start()
    try:
        statuses = await world.run_to_the_end([sample_group("g1", "S-1")])
    finally:
        await world.runtime.shutdown()

    assert world.pairings.seen == [("S-1", "IDX-1")] and world.pairings.adapters == ["AD-1"]
    assert all(status == "COMPLETED" for status in statuses.values()), statuses


async def test_a_plate_relabelled_after_it_was_bound_does_not_run_the_step() -> None:
    def relabel(ctx: ThreadContext) -> None:
        ctx.labware.barcode = "RELABELLED"

    world = await build_pairing_world({"S-1": "IDX-1"}, index_thread_first=relabel)
    await world.register("sample", "S-1")
    await world.register("index", "IDX-1")
    await world.runtime.start()
    try:
        _, paused = await _paused_sample(world, [sample_group("g1", "S-1")])
    finally:
        await world.runtime.shutdown()

    assert "needs the 'index' with barcode 'IDX-1'" in (paused.last_error or "")
    assert world.pairings.seen == []


async def test_a_later_run_is_served_by_the_plate_an_earlier_run_used() -> None:
    world = await build_pairing_world({"S-1": "IDX-1", "S-2": "IDX-1"}, index_stays_at_waste=True)
    for barcode in ("S-1", "S-2"):
        await world.register("sample", barcode)
    await world.register("index", "IDX-1")
    await world.runtime.start()
    try:
        first = await world.run_to_the_end([sample_group("g1", "S-1")])
        second = await world.run_to_the_end([sample_group("g2", "S-2")])
    finally:
        await world.runtime.shutdown()

    assert world.pairings.seen == [("S-1", "IDX-1"), ("S-2", "IDX-1")]
    assert all(status == "COMPLETED" for status in (first | second).values()), (first, second)


async def test_a_run_paused_and_resumed_still_pairs_each_sample_with_its_own() -> None:
    world = await build_pairing_world({"S-1": "IDX-1", "S-2": "IDX-2"})
    for barcode in ("S-1", "S-2"):
        await world.register("sample", barcode)
    for barcode in ("IDX-1", "IDX-2"):
        await world.register("index", barcode)
    await world.runtime.start()
    try:
        execution_id = await world.submit([sample_group("g1", "S-1"), sample_group("g2", "S-2")])
        world.runtime.pause_execution(execution_id)
        world.runtime.resume_execution(execution_id)
        statuses = await world.wait_for_the_end(execution_id)
    finally:
        await world.runtime.shutdown()

    assert sorted(world.pairings.seen, key=repr) == [("S-1", "IDX-1"), ("S-2", "IDX-2")]
    assert all(status == "COMPLETED" for status in statuses.values()), statuses


async def test_a_submission_that_joins_a_run_is_paired_with_the_plate_it_names() -> None:
    world = await build_pairing_world({"S-1": "IDX-1", "S-2": "IDX-2"})
    for barcode in ("S-1", "S-2"):
        await world.register("sample", barcode)
    await world.register("index", "IDX-2")
    await world.runtime.start()
    try:
        execution_id, paused = await _paused_sample(world, [sample_group("g1", "S-1")])
        joined = await world.submit([sample_group("g2", "S-2")], batch_mode=BatchMode.JOIN_EXISTING)
        assert joined == execution_id

        await world.register("index", "IDX-1")
        world.runtime.recover_thread(execution_id, paused.id, RecoveryDecision.RETRY)
        statuses = await world.wait_for_the_end(execution_id)
    finally:
        await world.runtime.shutdown()

    assert sorted(world.pairings.seen, key=repr) == [("S-1", "IDX-1"), ("S-2", "IDX-2")]
    assert all(status == "COMPLETED" for status in statuses.values()), statuses


async def test_a_group_that_names_its_partner_plate_in_the_submission_is_served_by_it() -> None:
    world = await build_pairing_world({})
    for barcode in ("S-1", "S-2"):
        await world.register("sample", barcode)
    for barcode in ("IDX-1", "IDX-2"):
        await world.register("index", barcode)
    await world.runtime.start()
    try:
        statuses = await world.run_to_the_end(
            [sample_group("g1", "S-1", index_barcode="IDX-2"), sample_group("g2", "S-2", index_barcode="IDX-1")],
        )
    finally:
        await world.runtime.shutdown()

    assert sorted(world.pairings.seen, key=repr) == [("S-1", "IDX-2"), ("S-2", "IDX-1")]
    assert all(status == "COMPLETED" for status in statuses.values()), statuses


async def test_a_partner_every_group_shares_cannot_be_named_by_one_group() -> None:
    world = await build_pairing_world({}, index_is_shared_by_every_group=True)
    await world.register("sample", "S-1")
    await world.register("index", "IDX-1")
    await world.runtime.start()
    try:
        with pytest.raises(AcquisitionValidationError, match="cannot choose its labware"):
            await world.submit([sample_group("g1", "S-1", index_barcode="IDX-1")])
    finally:
        await world.runtime.shutdown()


async def test_a_refusal_while_handing_a_step_to_the_plates_next_thread_is_retried_on_resume() -> None:
    first_tag_done = asyncio.Event()
    used = {"up": False}

    async def can_continue(labware: LabwareInstance, demand: DeclaredTracking | None) -> bool:
        return not used["up"]

    def the_plate_runs_out(ctx: ThreadContext) -> None:
        used["up"] = True
        first_tag_done.set()

    world = await build_pairing_world(
        {"S-1": "IDX-1"}, tags_per_sample=2, index_serves_one_tag_then_goes_home=True,
        index_can_continue=can_continue, index_thread_last=the_plate_runs_out, between_tags=first_tag_done.wait,
    )
    await world.register("sample", "S-1")
    await world.register("index", "IDX-1")
    await world.runtime.start()
    try:
        execution_id, paused = await _paused_sample(world, [sample_group("g1", "S-1")])
        assert paused.pause_reason == "system" and world.pairings.seen == [("S-1", "IDX-1")]

        used["up"] = False
        world.runtime.resume_execution(execution_id)
        statuses = await world.wait_for_the_end(execution_id)
    finally:
        await world.runtime.shutdown()

    assert world.pairings.seen == [("S-1", "IDX-1"), ("S-1", "IDX-1")]
    assert all(status == "COMPLETED" for status in statuses.values()), statuses


async def test_a_partner_named_in_the_submission_serves_a_step_owned_by_a_spawned_thread() -> None:
    world = await build_pairing_world({}, adapter_owns_the_index_step=True)
    await world.register("sample", "S-1")
    for barcode in ("IDX-2", "IDX-1"):
        await world.register("index", barcode)
    await world.runtime.start()
    try:
        statuses = await world.run_to_the_end([sample_group("g1", "S-1", index_barcode="IDX-1")])
    finally:
        await world.runtime.shutdown()

    assert world.pairings.loaded == ["IDX-1"]
    assert all(status == "COMPLETED" for status in statuses.values()), statuses


async def test_a_retry_after_two_threads_named_different_partners_does_not_skip_the_step(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="orca")

    def times_paused_for_the_conflict() -> int:
        return sum("two different 'adapter' labware" in record.getMessage() for record in caplog.records)

    world = await build_pairing_world(
        {"S-1": "IDX-1"},
        adapter_for_sample={"S-1": "AD-1"},
        index_thread_first=lambda ctx: ctx.set_partner_constraint("adapter", {"barcode": "AD-2"}),
    )
    await world.register("sample", "S-1")
    await world.register("index", "IDX-1")
    for barcode in ("AD-1", "AD-2"):
        await world.register("adapter", barcode)
    await world.runtime.start()
    try:
        execution_id = await world.submit([sample_group("g1", "S-1")])
        paused = await world.wait_for_a_pause(execution_id, "index")
        assert "two different 'adapter' labware" in (paused.last_error or "")

        world.runtime.recover_thread(execution_id, paused.id, RecoveryDecision.RETRY)
        await wait_until(
            lambda: times_paused_for_the_conflict() == 2, timeout=90.0, message="the retry never paused again",
        )
        again = await world.wait_for_a_pause(execution_id, "index")
    finally:
        await world.runtime.shutdown()

    # The conflict stands, so the thread is paused on the same step again: it did not move on to the next one.
    assert again.status == "PAUSED" and "two different 'adapter' labware" in (again.last_error or "")
    assert again.completed_method_count == 0
    assert world.pairings.seen == []


async def test_a_thread_waiting_for_a_plate_in_use_at_a_station_with_no_spare_site_is_served_after_a_retry() -> None:
    world = await build_pairing_world({"S-1": "IDX-1", "S-2": "IDX-1"}, station_sites=2, index_stays_at_waste=True)
    for barcode in ("S-1", "S-2"):
        await world.register("sample", barcode)
    await world.register("index", "IDX-1")
    await world.runtime.start()
    try:
        execution_id, paused = await _paused_sample(world, [sample_group("g1", "S-1"), sample_group("g2", "S-2")])
        assert "is in use by thread" in (paused.last_error or "")
        await wait_until(
            lambda: len(world.pairings.seen) == 1 and all(
                thread.status == "COMPLETED"
                for name, thread in world.threads(execution_id).items() if name.startswith("index")
            ),
            timeout=90.0, message="the first pair never finished",
        )
        world.runtime.recover_thread(execution_id, paused.id, RecoveryDecision.RETRY)
        statuses = await world.wait_for_the_end(execution_id)
    finally:
        await world.runtime.shutdown()

    assert sorted(world.pairings.seen, key=repr) == [("S-1", "IDX-1"), ("S-2", "IDX-1")]
    assert all(status == "COMPLETED" for status in statuses.values()), statuses


async def test_a_run_aborted_while_a_thread_waits_for_its_start_pad_ends_that_thread() -> None:
    world = await build_pairing_world({"S-1": "IDX-1", "S-2": "IDX-2"})
    for barcode in ("S-1", "S-2"):
        await world.register("sample", barcode)
    await world.register("index", "IDX-2")
    await world.runtime.start()
    try:
        execution_id, _ = await _paused_sample(world, [sample_group("g1", "S-1"), sample_group("g2", "S-2")])

        def waiting_for_the_pad() -> list[ThreadSnapshot]:
            return [
                thread for thread in world.threads(execution_id).values()
                if thread.status == "AWAITING_START_LOCATION"
            ]

        await wait_until(lambda: bool(waiting_for_the_pad()), timeout=60.0, message="no sample waited for its pad")
        [waiting] = waiting_for_the_pad()
        await world.runtime.abort_execution(execution_id)

        await wait_until(
            lambda: world.threads(execution_id)[waiting.name].status in TERMINAL_STATUSES,
            timeout=30.0, message="the thread waiting for its pad was still running after the abort",
        )
    finally:
        await world.runtime.shutdown()


async def test_a_run_aborted_while_a_thread_is_paused_for_its_partner_ends_every_thread() -> None:
    world = await build_pairing_world({"S-1": "IDX-1", "S-2": "IDX-2"})
    for barcode in ("S-1", "S-2"):
        await world.register("sample", barcode)
    await world.register("index", "IDX-2")
    await world.runtime.start()
    try:
        execution_id, _ = await _paused_sample(world, [sample_group("g1", "S-1"), sample_group("g2", "S-2")])
        await world.runtime.abort_execution(execution_id)

        def nothing_is_running() -> bool:
            return all(
                thread.status in TERMINAL_STATUSES or thread.status == "CREATED"
                for thread in world.threads(execution_id).values()
            )

        await wait_until(nothing_is_running, timeout=90.0, message="a thread was still running after the abort")
    finally:
        await world.runtime.shutdown()

    assert ("S-1", "IDX-1") not in world.pairings.seen


async def test_two_groups_naming_one_plate_that_every_group_shares_are_served_by_one_thread() -> None:
    world = await build_pairing_world({"S-1": "IDX-1", "S-2": "IDX-1"}, index_is_shared_by_every_group=True)
    for barcode in ("S-1", "S-2"):
        await world.register("sample", barcode)
    await world.register("index", "IDX-1")
    await world.runtime.start()
    try:
        execution_id = await world.submit([sample_group("g1", "S-1"), sample_group("g2", "S-2")])
        statuses = await world.wait_for_the_end(execution_id)
    finally:
        await world.runtime.shutdown()

    assert sorted(world.pairings.seen, key=repr) == [("S-1", "IDX-1"), ("S-2", "IDX-1")]
    assert len([name for name in statuses if name.startswith("index")]) == 1
    assert all(status == "COMPLETED" for status in statuses.values()), statuses


async def test_labware_every_group_shares_serves_groups_that_named_different_partners() -> None:
    world = await build_pairing_world({}, adapter_for_sample={}, adapter_is_shared_by_every_group=True, station_sites=4)
    for barcode in ("S-1", "S-2"):
        await world.register("sample", barcode)
    for barcode in ("IDX-1", "IDX-2"):
        await world.register("index", barcode)
    await world.runtime.start()
    try:
        statuses = await world.run_to_the_end(
            [sample_group("g1", "S-1", index_barcode="IDX-1"), sample_group("g2", "S-2", index_barcode="IDX-2")],
        )
    finally:
        await world.runtime.shutdown()

    assert sorted(world.pairings.seen, key=repr) == [("S-1", "IDX-1"), ("S-2", "IDX-2")]
    assert all(status == "COMPLETED" for status in statuses.values()), statuses


@pytest.mark.parametrize("resume", ["one thread", "every thread"])
async def test_a_refusal_during_a_handoff_is_retried_however_the_run_is_resumed(resume: str) -> None:
    first_tag_done = asyncio.Event()
    used = {"up": False}

    async def can_continue(labware: LabwareInstance, demand: DeclaredTracking | None) -> bool:
        return not used["up"]

    def the_plate_runs_out(ctx: ThreadContext) -> None:
        used["up"] = True
        first_tag_done.set()

    world = await build_pairing_world(
        {"S-1": "IDX-1"}, tags_per_sample=2, index_serves_one_tag_then_goes_home=True,
        index_can_continue=can_continue, index_thread_last=the_plate_runs_out, between_tags=first_tag_done.wait,
    )
    await world.register("sample", "S-1")
    await world.register("index", "IDX-1")
    await world.runtime.start()
    try:
        execution_id, paused = await _paused_sample(world, [sample_group("g1", "S-1")])
        used["up"] = False
        if resume == "one thread":
            world.runtime.resume_thread(execution_id, paused.id)
        else:
            world.runtime.resume_all_threads(execution_id)
        statuses = await world.wait_for_the_end(execution_id)
    finally:
        await world.runtime.shutdown()

    assert world.pairings.seen == [("S-1", "IDX-1"), ("S-1", "IDX-1")]
    assert all(status == "COMPLETED" for status in statuses.values()), statuses


async def test_a_step_whose_owner_thread_was_aborted_is_not_served_later() -> None:
    world = await build_pairing_world({"S-1": "IDX-1", "S-2": "IDX-2"})
    await world.register("sample", "S-1")
    await world.register("sample", "S-2")
    await world.runtime.start()
    try:
        execution_id = await world.submit([sample_group("g1", "S-1"), sample_group("g2", "S-2")])
        paused = await world.wait_for_a_pause(execution_id, "sample")
        owner = world.runtime._find_thread(execution_id, paused.id)
        step = owner.assigned_method
        named = owner.labware.barcode
        assert step is not None and named is not None
        world.runtime.recover_thread(execution_id, owner.id, RecoveryDecision.ABORT_THREAD)
        await owner.completed.wait()
        await world.register("index", "IDX-" + named[-1])
        workflow = world.runtime._get_execution(execution_id).executing_workflow
        assert workflow is not None

        await workflow._serve_or_leave_unserved("index", step, WorkflowRunMode.CHECK_RUN)

        index_threads = _threads_named(world, execution_id, "index")
    finally:
        await world.runtime.shutdown()

    assert index_threads == []


def test_a_named_slot_key_keeps_its_scope_and_survives_separators_in_the_barcode() -> None:
    key = named_partner_slot_key("index:g1:sub-1", "A.B:C")

    assert key.count(":") == 3 and "." not in key
    assert _decompose_slot_scope(key) == ("g1", "sub-1")
    assert _decompose_slot_scope(named_partner_slot_key("index", "IDX-1")) == (None, None)
    assert named_partner_slot_key("index:g1:sub-1", "IDX-1") != named_partner_slot_key("index:g1:sub-1", "IDX-2")


@pytest.mark.parametrize("constraints", [{}, {"barcode": ""}, {"barcode": " "}, {"lot": "L1"}, {"barcode": "B", "lot": "L1"}])
def test_a_partner_constraint_is_a_barcode_and_nothing_else(constraints: dict[str, str]) -> None:
    named: list[tuple[str, dict[str, str]]] = []
    ctx = ThreadContext.__new__(ThreadContext)
    ctx._partner_constraint_setter = lambda template, given: named.append((template, given))

    with pytest.raises(ValueError, match="must be \\{'barcode'"):
        ctx.set_partner_constraint("index", constraints)
    assert named == []


def _group_naming_threads_by_function(group_id: str, sample_barcode: str, index_barcode: str) -> LabwareGroup:
    return LabwareGroup(id=group_id, members=(
        LabwareGroupMember(thread_template_name="_sample_thread", acquisition=BarcodeAcquisition(barcode=sample_barcode)),
        LabwareGroupMember(thread_template_name="_index_thread", acquisition=BarcodeAcquisition(barcode=index_barcode)),
    ))


async def test_a_group_that_names_its_threads_by_function_name_is_served_by_the_plates_it_names() -> None:
    world = await build_pairing_world({})
    for barcode in ("S-1", "S-2"):
        await world.register("sample", barcode)
    for barcode in ("IDX-1", "IDX-2"):
        await world.register("index", barcode)
    await world.runtime.start()
    try:
        statuses = await world.run_to_the_end([
            _group_naming_threads_by_function("g1", "S-1", "IDX-2"),
            _group_naming_threads_by_function("g2", "S-2", "IDX-1"),
        ])
    finally:
        await world.runtime.shutdown()

    assert sorted(world.pairings.seen, key=repr) == [("S-1", "IDX-2"), ("S-2", "IDX-1")]
    assert all(status == "COMPLETED" for status in statuses.values()), statuses


async def test_a_partner_every_group_shares_cannot_be_named_by_one_group_by_its_function_name() -> None:
    world = await build_pairing_world({}, index_is_shared_by_every_group=True)
    await world.register("sample", "S-1")
    await world.register("index", "IDX-1")
    await world.runtime.start()
    try:
        with pytest.raises(AcquisitionValidationError, match="cannot choose its labware"):
            await world.submit([_group_naming_threads_by_function("g1", "S-1", "IDX-1")])
    finally:
        await world.runtime.shutdown()
