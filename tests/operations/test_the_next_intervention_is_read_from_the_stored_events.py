"""The next intervention comes from the stored events: the events a person must act on, in cursor order."""

import asyncio

import pytest
from pydantic import JsonValue

from orca.operations.events import IEventLog, ListEventsRequest, PastEvent
from orca.operations.interventions import NextInterventionOperation, NextInterventionRequest
from tests.test_helpers import wait_until


class _StoredEvents(IEventLog):
    def __init__(self) -> None:
        self.stored: list[PastEvent] = []
        self.reads = 0

    def store(self, name: str, context: dict[str, JsonValue] | None = None) -> int:
        entity_type, entity_id, status = name.split(".")
        cursor = len(self.stored) + 1
        self.stored.append(PastEvent(
            cursor=cursor, event_name=name, execution_id="exec-1", timestamp=0.0,
            entity_type=entity_type, entity_id=entity_id, status=status, context=context or {},
        ))
        return cursor

    async def events(self, query: ListEventsRequest) -> list[PastEvent]:
        self.reads += 1
        if query.tail:
            return self.stored[-query.limit:]
        return [e for e in self.stored if e.cursor > query.since][:query.limit]


def _next(log: _StoredEvents) -> NextInterventionOperation:
    return NextInterventionOperation(event_log=log, poll_interval=0.01)


async def test_an_event_nobody_must_act_on_is_skipped() -> None:
    log = _StoredEvents()
    log.store("THREAD.t1.MOVING")

    answer = await _next(log).run(NextInterventionRequest(after_seq=0, timeout_seconds=0.05))

    assert answer.intervention is None


async def test_a_manual_place_carries_what_the_operator_must_place_and_where() -> None:
    log = _StoredEvents()
    log.store("THREAD.t1.MOVING")
    cursor = log.store("THREAD.t1.AWAITING_MANUAL_PLACE", {
        "thread_id": "t1", "labware_id": "lw-1", "labware_name": "plate_1",
        "labware_template_name": "plate_96_well", "target_location": "hotel_A1",
    })

    answer = await _next(log).run(NextInterventionRequest(after_seq=0, timeout_seconds=1.0))

    assert answer.intervention is not None
    assert answer.intervention.kind == "MANUAL_PLACE"
    assert answer.intervention.labware_template_name == "plate_96_well"
    assert answer.intervention.target_location == "hotel_A1"
    assert answer.intervention.seq == cursor
    assert answer.next_cursor == cursor


async def test_an_incident_carries_its_summary() -> None:
    log = _StoredEvents()
    log.store("INCIDENT.inc-1.ACTION_FAILED", {
        "incident_id": "inc-1", "category": "ACTION_FAILED", "severity": "ERROR",
        "message": "dispense failed", "recovery_action": "THREAD_RECOVER_RETRY",
    })

    answer = await _next(log).run(NextInterventionRequest(after_seq=0, timeout_seconds=1.0))

    assert answer.intervention is not None
    assert answer.intervention.kind == "INCIDENT"
    assert answer.intervention.incident_id == "inc-1"
    assert answer.intervention.message == "dispense failed"
    assert answer.intervention.recovery_action == "THREAD_RECOVER_RETRY"


async def test_a_waiting_call_returns_an_intervention_stored_while_it_waits() -> None:
    log = _StoredEvents()
    waiting = asyncio.create_task(_next(log).run(NextInterventionRequest(after_seq=0, timeout_seconds=5.0)))
    await wait_until(lambda: log.reads >= 2)
    log.store("INCIDENT.inc-live.ACTION_FAILED")

    answer = await waiting

    assert answer.intervention is not None
    assert answer.intervention.event_name == "INCIDENT.inc-live.ACTION_FAILED"


async def test_following_the_cursor_misses_no_intervention() -> None:
    log = _StoredEvents()
    log.store("INCIDENT.a.ACTION_FAILED")
    log.store("THREAD.t1.MOVING")
    log.store("INCIDENT.b.ACTION_FAILED")

    first = await _next(log).run(NextInterventionRequest(after_seq=0, timeout_seconds=1.0))
    second = await _next(log).run(NextInterventionRequest(after_seq=first.next_cursor, timeout_seconds=1.0))

    assert first.intervention is not None and first.intervention.event_name == "INCIDENT.a.ACTION_FAILED"
    assert second.intervention is not None and second.intervention.event_name == "INCIDENT.b.ACTION_FAILED"


async def test_no_cursor_waits_only_for_interventions_from_now() -> None:
    log = _StoredEvents()
    log.store("INCIDENT.old.ACTION_FAILED")

    answer = await _next(log).run(NextInterventionRequest(timeout_seconds=0.05))

    assert answer.intervention is None
    assert answer.next_cursor == 1


async def test_a_timeout_moves_the_cursor_past_what_it_read_and_the_next_call_finds_a_late_intervention() -> None:
    log = _StoredEvents()
    first = log.store("INCIDENT.a.ACTION_FAILED")
    read = log.store("THREAD.t1.MOVING")

    timed_out = await _next(log).run(NextInterventionRequest(after_seq=first, timeout_seconds=0.05))
    late = log.store("INCIDENT.b.ACTION_FAILED")
    found = await _next(log).run(NextInterventionRequest(after_seq=timed_out.next_cursor, timeout_seconds=1.0))

    assert timed_out.intervention is None
    assert timed_out.next_cursor == read
    assert found.intervention is not None and found.next_cursor == late


async def test_more_stored_events_than_one_page_are_all_read() -> None:
    log = _StoredEvents()
    for i in range(1200):
        log.store(f"THREAD.t{i}.MOVING")
    cursor = log.store("OPERATOR.op.INSTRUCTION", {"instruction": "swap the trough", "step_id": "s1"})

    answer = await _next(log).run(NextInterventionRequest(after_seq=0, timeout_seconds=1.0))

    assert answer.intervention is not None
    assert answer.intervention.kind == "MANUAL_STEP"
    assert answer.intervention.instruction == "swap the trough"
    assert answer.next_cursor == cursor


@pytest.mark.parametrize("timeout", [0.0, 601.0])
def test_a_timeout_outside_its_range_is_refused(timeout: float) -> None:
    with pytest.raises(ValueError):
        NextInterventionRequest(timeout_seconds=timeout)
