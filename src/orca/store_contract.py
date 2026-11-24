"""What every implementation of an activity store interface must do.

Internal test support, shared by the store tests of every implementation; not part
of the public import surface. Each ``*_STORE_CHECKS`` tuple holds async checks
for one interface. Run every check against a fresh, empty store whose schema
exists; a check raises ``AssertionError`` when the store breaks the contract.
"""

import asyncio
import dataclasses
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import TypeVar

from orca.events.execution_context import WorkflowExecutionContext
from orca.events.runtime_event import RuntimeEvent
from orca.modes.device_modes import DeviceMode, IDeviceModes
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.store_interfaces import ILabwareStore
from orca.runtime.audit_store import AuditEntry, AuditQuery, IAuditStore
from orca.runtime.errors import BarcodeInUseError
from orca.runtime.event_store import EventQuery, IEventStore
from orca.runtime.execution_record import ExecutionState
from orca.runtime.execution_record_store import IExecutionRecordStore, PersistedThread
from orca.runtime.incident_store import (
    IIncidentStore,
    IncidentCategory,
    IncidentSeverity,
    OtherIncidentDetail,
    RecoveryAction,
    SystemIncident,
    VariableResolutionDetail,
    build_incident,
)
from orca.state.ops_store import IOpsHistoryStore, OpsHistorySearchQuery
from orca.state.records import (
    AspirateDetails,
    DeviceOperation,
    DispenseDetails,
    OperationRecord,
    TrackingRecord,
    TrackingSource,
)

T = TypeVar("T")
S = TypeVar("S")
E = TypeVar("E", bound=Exception)

StoreCheck = Callable[[S], Awaitable[None]]


def _expect(actual: T, expected: T, what: str) -> None:
    if actual != expected:
        raise AssertionError(f"{what}: expected {expected!r}, got {actual!r}")


async def _expect_raises(error: type[E], call: Awaitable[T], what: str) -> E:
    try:
        await call
    except error as raised:
        return raised
    raise AssertionError(f"{what}: expected {error.__name__}, nothing was raised")


# Labware


async def a_registered_labware_is_found_by_id_and_barcode(store: ILabwareStore) -> None:
    plate = LabwareInstance(template_name="plate_96", labware_type="plate", barcode="BC-1")
    plate.carry_with(z_offset=2.0)
    await store.register(plate, execution_id="exec-1")

    found = await store.get_by_id(plate.id)
    if found is None:
        raise AssertionError("get_by_id: the registered labware is missing")
    _expect(
        (found.id, found.name, found.template_name, found.labware_type, found.barcode),
        (plate.id, plate.name, "plate_96", "plate", "BC-1"),
        "get_by_id",
    )
    _expect(found.carry_override, plate.carry_override, "get_by_id carry override")
    by_barcode = await store.get_by_barcode("BC-1")
    _expect(None if by_barcode is None else by_barcode.id, plate.id, "get_by_barcode")
    _expect(await store.get_by_id("never-registered"), None, "get_by_id of an unknown id")
    _expect(await store.get_by_barcode("BC-unknown"), None, "get_by_barcode of an unknown barcode")


async def registering_again_updates_the_labware(store: ILabwareStore) -> None:
    plate = LabwareInstance(template_name="plate_96", labware_type="plate", barcode="BC-OLD")
    await store.register(plate)
    plate.barcode = "BC-NEW"
    plate.carry_with(speed=30.0)
    await store.register(plate, execution_id="exec-2")

    found = await store.get_by_barcode("BC-NEW")
    _expect(None if found is None else found.id, plate.id, "get_by_barcode after re-register")
    _expect(None if found is None else found.carry_override, plate.carry_override, "carry override after re-register")
    _expect(await store.get_by_barcode("BC-OLD"), None, "the old barcode after re-register")


async def a_second_labware_cannot_take_a_held_barcode(store: ILabwareStore) -> None:
    holder = LabwareInstance(template_name="plate_96", labware_type="plate", barcode="BC-A")
    other = LabwareInstance(template_name="plate_96", labware_type="plate", barcode="BC-A")
    await store.register(holder)

    raised = await _expect_raises(BarcodeInUseError, store.register(other), "register with a held barcode")
    _expect(raised.existing_labware_id, holder.id, "BarcodeInUseError names the holder")
    _expect(await store.get_by_id(other.id), None, "the refused labware")


async def a_moved_labware_is_found_at_its_position(store: ILabwareStore) -> None:
    plate = LabwareInstance(template_name="plate_96", labware_type="plate")
    parked = LabwareInstance(template_name="plate_96", labware_type="plate")
    await store.register(plate)
    await store.register(parked)
    _expect(await store.list_active_locations(), [], "active locations before any move")

    await store.update_location(plate.id, "pad_1")
    await store.update_location(plate.id, "pad_2")

    at_pad = await store.get_by_position("pad_2")
    _expect(None if at_pad is None else at_pad.id, plate.id, "get_by_position")
    _expect(await store.get_by_position("pad_9"), None, "get_by_position of an empty position")
    _expect(await store.list_active_locations(), [(plate.id, "pad_2")], "active locations")


async def the_latest_registered_labware_wins_a_shared_position(store: ILabwareStore) -> None:
    older = LabwareInstance(template_name="plate_96", labware_type="plate")
    newer = LabwareInstance(template_name="plate_96", labware_type="plate")
    await store.register(older)
    # Registration time orders them; the gap is wider than any clock's resolution.
    await asyncio.sleep(0.05)
    await store.register(newer)
    await store.update_location(older.id, "pad_1")
    await store.update_location(newer.id, "pad_1")

    found = await store.get_by_position("pad_1")
    _expect(None if found is None else found.id, newer.id, "get_by_position with two labware at one position")


async def a_move_of_unknown_labware_is_refused(store: ILabwareStore) -> None:
    await _expect_raises(KeyError, store.update_location("never-registered", "pad_1"), "update_location")


async def a_cleared_labware_keeps_its_row_and_history(store: ILabwareStore) -> None:
    plate = LabwareInstance(template_name="plate_96", labware_type="plate")
    await store.register(plate)
    await store.update_location(plate.id, "pad_1")

    await store.clear_location(plate.id)
    await store.clear_location(plate.id)
    await store.clear_location("never-registered")

    _expect(await store.list_active_locations(), [], "active locations after a clear")
    _expect(await store.get_by_position("pad_1"), None, "get_by_position after a clear")
    found = await store.get_by_id(plate.id)
    _expect(None if found is None else found.id, plate.id, "get_by_id after a clear")
    history = await store.get_location_history(plate.id)
    _expect([(to, previous) for to, previous, _ in history], [("pad_1", None)], "history after a clear")


async def a_cleared_labware_is_listed_at_its_last_position(store: ILabwareStore) -> None:
    gone = LabwareInstance(template_name="plate_96", labware_type="plate")
    back = LabwareInstance(template_name="plate_96", labware_type="plate")
    kept = LabwareInstance(template_name="plate_96", labware_type="plate")
    for plate in (gone, back, kept):
        await store.register(plate)
    await store.update_location(gone.id, "pad_1")
    await store.update_location(gone.id, "pad_2")
    await store.clear_location(gone.id)
    await store.update_location(back.id, "pad_3")
    await store.clear_location(back.id)
    await store.update_location(back.id, "pad_4")
    await store.update_location(kept.id, "pad_5")

    _expect(await store.list_departed_locations(), [(gone.id, "pad_2")], "departed locations")


async def the_history_lists_each_move_with_its_utc_time(store: ILabwareStore) -> None:
    plate = LabwareInstance(template_name="plate_96", labware_type="plate")
    await store.register(plate)
    before = time.time()
    await store.update_location(plate.id, "pad_1")
    await store.update_location(plate.id, "pad_2")

    history = await store.get_location_history(plate.id)

    _expect(
        [(to, previous) for to, previous, _ in history], [("pad_1", None), ("pad_2", "pad_1")], "location history",
    )
    for _, _, stamped in history:
        if abs(stamped - before) > 60:
            raise AssertionError(f"location history: time {stamped} is not now ({before}); was it read as local time?")
    _expect(await store.get_location_history("never-registered"), [], "history of an unknown labware")


LABWARE_STORE_CHECKS: tuple[StoreCheck[ILabwareStore], ...] = (
    a_registered_labware_is_found_by_id_and_barcode,
    registering_again_updates_the_labware,
    a_second_labware_cannot_take_a_held_barcode,
    a_moved_labware_is_found_at_its_position,
    the_latest_registered_labware_wins_a_shared_position,
    a_move_of_unknown_labware_is_refused,
    a_cleared_labware_keeps_its_row_and_history,
    a_cleared_labware_is_listed_at_its_last_position,
    the_history_lists_each_move_with_its_utc_time,
)


# Incidents


def _incident(
    timestamp: float, *, category: IncidentCategory = IncidentCategory.OTHER, execution_id: str | None = None,
) -> SystemIncident:
    incident = build_incident(
        category=category,
        severity=IncidentSeverity.WARNING,
        message=f"incident at {timestamp}",
        detail=(
            VariableResolutionDetail(var_name="x", action_command="shake")
            if category is IncidentCategory.VARIABLE_RESOLUTION
            else OtherIncidentDetail(message_extra="extra")
        ),
        recovery_action=RecoveryAction.NONE,
        execution_id=execution_id,
        thread_id="thread-a",
    )
    return dataclasses.replace(incident, timestamp=timestamp)


async def an_inserted_incident_reads_back_whole(store: IIncidentStore) -> None:
    incident = _incident(100.0, category=IncidentCategory.VARIABLE_RESOLUTION, execution_id="exec-1")
    await store.insert(incident)

    _expect(await store.get(incident.id), incident, "get")
    _expect(await store.get("never-inserted"), None, "get of an unknown id")


async def fetch_filters_incidents_and_orders_them_by_time(store: IIncidentStore) -> None:
    late = _incident(300.0, execution_id="exec-1")
    early = _incident(100.0, category=IncidentCategory.VARIABLE_RESOLUTION, execution_id="exec-1")
    middle = _incident(200.0, execution_id="exec-2")
    for incident in (late, early, middle):
        await store.insert(incident)
    await store.mark_acked(middle.id, datetime(2026, 1, 1))

    def ids(found: list[SystemIncident]) -> list[str]:
        return [incident.id for incident in found]

    _expect(ids(await store.fetch()), [early.id, middle.id, late.id], "fetch")
    _expect(ids(await store.fetch(unacknowledged_only=True)), [early.id, late.id], "fetch unacknowledged")
    _expect(ids(await store.fetch(category=IncidentCategory.OTHER)), [middle.id, late.id], "fetch by category")
    _expect(ids(await store.fetch(execution_id="exec-1")), [early.id, late.id], "fetch by execution")
    _expect(ids(await store.fetch(since=200.0)), [middle.id, late.id], "fetch since")


async def acknowledging_marks_incidents_and_counts_them(store: IIncidentStore) -> None:
    one = _incident(100.0)
    two = _incident(200.0)
    variable = _incident(300.0, category=IncidentCategory.VARIABLE_RESOLUTION)
    for incident in (one, two, variable):
        await store.insert(incident)
    acknowledged_at = datetime(2026, 1, 1)

    await store.mark_acked(one.id, acknowledged_at)
    await store.mark_acked("never-inserted", acknowledged_at)
    acked_one = await store.get(one.id)
    _expect(None if acked_one is None else acked_one.acknowledged, True, "mark_acked")

    _expect(await store.mark_all_acked(IncidentCategory.OTHER, acknowledged_at), 1, "mark_all_acked by category")
    _expect([i.id for i in await store.fetch(unacknowledged_only=True)], [variable.id], "unacknowledged after it")
    _expect(await store.mark_all_acked(None, acknowledged_at), 1, "mark_all_acked")
    _expect(await store.mark_all_acked(None, acknowledged_at), 0, "mark_all_acked with nothing left")


INCIDENT_STORE_CHECKS: tuple[StoreCheck[IIncidentStore], ...] = (
    an_inserted_incident_reads_back_whole,
    fetch_filters_incidents_and_orders_them_by_time,
    acknowledging_marks_incidents_and_counts_them,
)


# Execution records

_SUBMITTED = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


async def a_running_record_is_kept_and_the_first_submission_wins(store: IExecutionRecordStore) -> None:
    await store.upsert_running("exec-1", "assay", _SUBMITTED)
    await store.upsert_running("exec-1", "other", _SUBMITTED + timedelta(hours=1))

    found = await store.get("exec-1")
    if found is None:
        raise AssertionError("get: the running record is missing")
    _expect((found.workflow_name, found.status, found.terminal_at), ("assay", ExecutionState.RUNNING, None), "get")
    _expect(await store.get("never-submitted"), None, "get of an unknown id")


async def a_terminal_mark_sets_the_outcome_and_says_whether_the_run_exists(store: IExecutionRecordStore) -> None:
    await store.upsert_running("exec-1", "assay", _SUBMITTED)
    ended = _SUBMITTED + timedelta(minutes=5)

    _expect(await store.mark_terminal("exec-1", ExecutionState.FAILED, "lost", ended), True, "mark_terminal")
    _expect(
        await store.mark_terminal("never-submitted", ExecutionState.FAILED, None, ended), False,
        "mark_terminal of an unknown id",
    )
    found = await store.get("exec-1")
    _expect(
        None if found is None else (found.status, found.terminal_reason),
        (ExecutionState.FAILED, "lost"), "the terminal record",
    )


async def times_are_stored_as_naive_utc(store: IExecutionRecordStore) -> None:
    two_hours_east = timezone(timedelta(hours=2))
    await store.upsert_running("exec-1", "assay", datetime(2026, 3, 1, 14, 0, tzinfo=two_hours_east))
    await store.mark_terminal(
        "exec-1", ExecutionState.COMPLETED, None, datetime(2026, 3, 1, 14, 30, tzinfo=two_hours_east),
    )

    found = await store.get("exec-1")
    _expect(
        None if found is None else (found.submitted_at, found.terminal_at),
        (datetime(2026, 3, 1, 12, 0), datetime(2026, 3, 1, 12, 30)), "stored times",
    )


async def runs_are_listed_newest_first_and_unfinished_ones_apart(store: IExecutionRecordStore) -> None:
    for offset, execution_id in enumerate(("exec-old", "exec-mid", "exec-new")):
        await store.upsert_running(execution_id, "assay", _SUBMITTED + timedelta(minutes=offset))
    await store.mark_terminal("exec-mid", ExecutionState.COMPLETED, None, _SUBMITTED + timedelta(hours=1))

    _expect([r.execution_id for r in await store.list_all()], ["exec-new", "exec-mid", "exec-old"], "list_all")
    _expect(
        sorted(r.execution_id for r in await store.list_non_terminal()), ["exec-new", "exec-old"], "list_non_terminal",
    )


async def thread_summaries_are_replaced_and_listed_by_id(store: IExecutionRecordStore) -> None:
    await store.upsert_running("exec-1", "assay", _SUBMITTED)
    second = PersistedThread(thread_id="t2", name="plate 2", template_name="journey", status="RUNNING")
    first = PersistedThread(thread_id="t1", name="plate 1", template_name="journey", status="RUNNING")
    first_paused = PersistedThread(
        thread_id="t1", name="plate 1", template_name="journey", status="PAUSED",
        last_error="jam", pause_reason="ERROR",
    )
    for thread in (second, first, first_paused):
        await store.upsert_thread("exec-1", thread)

    _expect(await store.list_threads("exec-1"), [first_paused, second], "list_threads")
    _expect(await store.list_threads("exec-other"), [], "list_threads of another run")


async def deleting_a_run_removes_its_threads(store: IExecutionRecordStore) -> None:
    for execution_id in ("exec-1", "exec-2"):
        await store.upsert_running(execution_id, "assay", _SUBMITTED)
        await store.upsert_thread(
            execution_id, PersistedThread(thread_id="t1", name="p", template_name="j", status="RUNNING"),
        )

    await store.delete("exec-1")
    await store.delete("never-submitted")

    _expect(await store.get("exec-1"), None, "get after delete")
    _expect(await store.list_threads("exec-1"), [], "threads after delete")
    _expect([r.execution_id for r in await store.list_all()], ["exec-2"], "the other run after delete")
    _expect(len(await store.list_threads("exec-2")), 1, "the other run's threads after delete")


EXECUTION_RECORD_STORE_CHECKS: tuple[StoreCheck[IExecutionRecordStore], ...] = (
    a_running_record_is_kept_and_the_first_submission_wins,
    a_terminal_mark_sets_the_outcome_and_says_whether_the_run_exists,
    times_are_stored_as_naive_utc,
    runs_are_listed_newest_first_and_unfinished_ones_apart,
    thread_summaries_are_replaced_and_listed_by_id,
    deleting_a_run_removes_its_threads,
)


# Operation record


def _operation(operation: DeviceOperation, labware: str, device: str) -> OperationRecord:
    details = (
        DispenseDetails(labware=labware, positions=["A1"], volumes=[10.0])
        if operation is DeviceOperation.DISPENSE
        else AspirateDetails(labware=labware, positions=["A1"], volumes=[10.0])
    )
    return OperationRecord(
        operation=operation, device_name=device, affected_labware=[labware],
        action_id="a", thread_id="t", details=details, timestamp=1.0,
    )


def _record(
    action_id: str, timestamp: float, *, thread_id: str, method_id: str,
    operations: list[OperationRecord] | None = None, source: TrackingSource = TrackingSource.OBSERVED,
) -> TrackingRecord:
    return TrackingRecord(
        execution_id="unused", action_id=action_id, thread_id=thread_id, method_id=method_id,
        source=source, timestamp=timestamp, operations=operations or [],
    )


async def records_are_listed_per_execution_in_write_order(store: IOpsHistoryStore) -> None:
    first, second, other = _record("a1", 200.0, thread_id="t1", method_id="m1"), _record("a2", 100.0, thread_id="t1", method_id="m1"), _record("b1", 50.0, thread_id="t1", method_id="m1")
    await store.append("exec-1", first)
    await store.append("exec-1", second)
    await store.append("exec-2", other)

    _expect(await store.list("exec-1"), [first, second], "list")
    _expect(await store.for_execution("exec-2").list(), [other], "for_execution().list")
    _expect(await store.list("never-written"), [], "list of an unknown execution")


async def search_is_in_time_order_and_filters_each_field(store: IOpsHistoryStore) -> None:
    late = _record("a1", 300.0, thread_id="t1", method_id="m1")
    early = _record("a2", 100.0, thread_id="t2", method_id="m2", source=TrackingSource.OPERATOR)
    other_run = _record("a3", 200.0, thread_id="t1", method_id="m1")
    await store.append("exec-1", late)
    await store.append("exec-1", early)
    await store.append("exec-2", other_run)

    def actions(found: list[tuple[str, TrackingRecord]]) -> list[tuple[str, str]]:
        return [(execution_id, record.action_id) for execution_id, record in found]

    _expect(
        actions(await store.search(OpsHistorySearchQuery())),
        [("exec-1", "a2"), ("exec-2", "a3"), ("exec-1", "a1")], "search",
    )
    _expect(actions(await store.search(OpsHistorySearchQuery(execution_id="exec-2"))), [("exec-2", "a3")], "by run")
    _expect(actions(await store.search(OpsHistorySearchQuery(action_id="a1"))), [("exec-1", "a1")], "by action")
    _expect(actions(await store.search(OpsHistorySearchQuery(thread_id="t2"))), [("exec-1", "a2")], "by thread")
    _expect(actions(await store.search(OpsHistorySearchQuery(method_id="m2"))), [("exec-1", "a2")], "by method")
    _expect(
        actions(await store.search(OpsHistorySearchQuery(source=TrackingSource.OPERATOR))), [("exec-1", "a2")],
        "by source",
    )


async def search_finds_records_by_labware_device_and_operation(store: IOpsHistoryStore) -> None:
    both = _record("a1", 100.0, thread_id="t1", method_id="m1", operations=[
        _operation(DeviceOperation.ASPIRATE, "plate_a", "lh_1"),
        _operation(DeviceOperation.DISPENSE, "plate_b", "lh_1"),
    ])
    elsewhere = _record("a2", 200.0, thread_id="t1", method_id="m1", operations=[_operation(DeviceOperation.ASPIRATE, "plate_c", "lh_2")])
    await store.append("exec-1", both)
    await store.append("exec-1", elsewhere)

    def actions(found: list[tuple[str, TrackingRecord]]) -> list[str]:
        return [record.action_id for _, record in found]

    _expect(actions(await store.search(OpsHistorySearchQuery(labware_name="plate_b"))), ["a1"], "by labware")
    _expect(actions(await store.search(OpsHistorySearchQuery(labware_name="plate_z"))), [], "by unknown labware")
    _expect(actions(await store.search(OpsHistorySearchQuery(device_name="lh_1"))), ["a1"], "by device")
    _expect(actions(await store.search(OpsHistorySearchQuery(device_name="lh_2"))), ["a2"], "by the other device")
    _expect(
        actions(await store.search(OpsHistorySearchQuery(operation=DeviceOperation.DISPENSE))), ["a1"], "by operation",
    )


OPS_HISTORY_STORE_CHECKS: tuple[StoreCheck[IOpsHistoryStore], ...] = (
    records_are_listed_per_execution_in_write_order,
    search_is_in_time_order_and_filters_each_field,
    search_finds_records_by_labware_device_and_operation,
)


# Device modes


async def a_device_with_no_setting_is_sim(store: IDeviceModes) -> None:
    _expect(await store.mode_of("never-set"), DeviceMode.SIM, "mode_of an unset device")


async def a_setting_is_kept_and_replaced(store: IDeviceModes) -> None:
    await store.set("pf400_1", DeviceMode.LIVE)
    _expect(await store.mode_of("pf400_1"), DeviceMode.LIVE, "mode_of after set")
    _expect(await store.mode_of("shaker_1"), DeviceMode.SIM, "mode_of another device")
    await store.set("pf400_1", DeviceMode.SIM)
    _expect(await store.mode_of("pf400_1"), DeviceMode.SIM, "mode_of after a second set")


DEVICE_MODE_STORE_CHECKS: tuple[StoreCheck[IDeviceModes], ...] = (
    a_device_with_no_setting_is_sim,
    a_setting_is_kept_and_replaced,
)


# Audit


def _audit_entry(timestamp: float, action_name: str = "thread.skip_method", danger_level: str = "CRITICAL") -> AuditEntry:
    return AuditEntry(
        timestamp=timestamp, action_name=action_name, danger_level=danger_level, reason="because",
        call_args={"thread_id": "t1", "count": 2},
    )


async def audit_entries_read_back_whole_and_newest_first(store: IAuditStore) -> None:
    first, second = _audit_entry(100.0), _audit_entry(200.0, "thread.abort")
    await store.append(first)
    await store.append(second)

    _expect(await store.newest_first(AuditQuery()), [second, first], "newest_first")
    _expect(await store.newest_first(AuditQuery(limit=1)), [second], "newest_first with a limit")


async def audit_entries_are_filtered_by_each_field(store: IAuditStore) -> None:
    skip = _audit_entry(100.0)
    abort = _audit_entry(200.0, "thread.abort", "DANGEROUS")
    late = _audit_entry(300.0)
    for entry in (skip, abort, late):
        await store.append(entry)

    _expect(await store.newest_first(AuditQuery(action_name="thread.abort")), [abort], "by action")
    _expect(await store.newest_first(AuditQuery(danger_level="CRITICAL")), [late, skip], "by danger level")
    _expect(await store.newest_first(AuditQuery(since=200.0)), [late, abort], "since")
    _expect(await store.newest_first(AuditQuery(until=200.0)), [abort, skip], "until")


AUDIT_STORE_CHECKS: tuple[StoreCheck[IAuditStore], ...] = (
    audit_entries_read_back_whole_and_newest_first,
    audit_entries_are_filtered_by_each_field,
)


# Events


def _event(name: str, execution_id: str) -> RuntimeEvent:
    return RuntimeEvent.from_event_bus(
        name, execution_id, WorkflowExecutionContext(execution_id=execution_id, workflow_name="assay"),
    )


async def a_stored_event_keeps_every_field(store: IEventStore) -> None:
    event = _event("THREAD.t1.PAUSED", "exec-1")
    await store.append(event)

    stored = await store.read(EventQuery())
    _expect(len(stored), 1, "events stored")
    _expect(RuntimeEvent.model_validate(stored[0].details), event, "stored details")
    _expect(
        (stored[0].event_name, stored[0].execution_id, stored[0].entity_type, stored[0].entity_id, stored[0].timestamp),
        ("THREAD.t1.PAUSED", "exec-1", "THREAD", "t1", event.timestamp), "stored columns",
    )


async def events_page_from_a_cursor_and_tail_takes_the_newest(store: IEventStore) -> None:
    for i in range(4):
        await store.append(_event(f"THREAD.t{i}.RUNNING", "exec-1" if i % 2 == 0 else "exec-2"))

    first_two = await store.read(EventQuery(limit=2))
    rest = await store.read(EventQuery(since=first_two[-1].cursor))
    _expect([e.entity_id for e in first_two + rest], ["t0", "t1", "t2", "t3"], "paged reads")
    _expect([e.entity_id for e in await store.read(EventQuery(tail=True, limit=2))], ["t2", "t3"], "tail")
    _expect([e.entity_id for e in await store.read(EventQuery(execution_id="exec-2"))], ["t1", "t3"], "by run")


EVENT_STORE_CHECKS: tuple[StoreCheck[IEventStore], ...] = (
    a_stored_event_keeps_every_field,
    events_page_from_a_cursor_and_tail_takes_the_newest,
)
