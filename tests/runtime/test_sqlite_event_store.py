"""SqliteEventStore keeps every event's full details and pages them by cursor."""

from pathlib import Path

from orca.events.execution_context import WorkflowExecutionContext
from orca.events.runtime_event import RuntimeEvent
from orca.runtime.event_store import EventQuery
from orca.runtime.sqlite_event_store import SqliteEventStore
from tests.ephemeral_ops_store import activity_engine


def _event(name: str, execution_id: str) -> RuntimeEvent:
    return RuntimeEvent.from_event_bus(
        name, execution_id, WorkflowExecutionContext(execution_id=execution_id, workflow_name="smc"),
    )


async def test_a_stored_event_keeps_every_field_it_was_emitted_with(tmp_path: Path) -> None:
    store = SqliteEventStore(activity_engine(tmp_path / "state.db", ("runtime_events",)))
    event = _event("THREAD.t1.PAUSED", "exec-1")
    await store.append(event)

    [stored] = await store.read(EventQuery())

    assert RuntimeEvent.model_validate(stored.details) == event
    assert (stored.event_name, stored.entity_type, stored.entity_id) == ("THREAD.t1.PAUSED", "THREAD", "t1")


async def test_reads_page_forward_from_a_cursor_and_tail_takes_the_newest(tmp_path: Path) -> None:
    store = SqliteEventStore(activity_engine(tmp_path / "state.db", ("runtime_events",)))
    for i in range(4):
        await store.append(_event(f"THREAD.t{i}.RUNNING", "exec-1" if i % 2 == 0 else "exec-2"))

    first_two = await store.read(EventQuery(limit=2))
    rest = await store.read(EventQuery(since=first_two[-1].cursor))
    newest = await store.read(EventQuery(tail=True, limit=2))
    one_run = await store.read(EventQuery(execution_id="exec-2"))

    assert [e.entity_id for e in first_two + rest] == ["t0", "t1", "t2", "t3"]
    assert [e.entity_id for e in newest] == ["t2", "t3"]
    assert [e.entity_id for e in one_run] == ["t1", "t3"]
