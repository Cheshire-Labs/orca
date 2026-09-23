"""Off-loop incident recording (driver callbacks fire from executor threads).

Pins that ``record()`` is safe off the event loop: it does not raise, it emits
at record-time via the ``on_record`` seam, and the incident persists -- via
read-your-writes flush on read, via the event-driven background drain, and via
``stop_drain_task`` for anything still queued at teardown. Also pins that the
real loop-bound sinks (``SseEventSink``) and the ``SystemEventBus`` itself
survive an off-loop emit without losing or corrupting events.
"""

import asyncio

from orca.daemon.event_stream import SseEventSink
from orca.events.execution_context import IncidentContext
from orca.events.runtime_event import RuntimeEvent
from orca.runtime.db import create_memory_engine
from orca.runtime.incident_service import IncidentService
from orca.runtime.incident_store import (
    IncidentCategory,
    IncidentSeverity,
    OtherIncidentDetail,
    RecoveryAction,
    SystemIncident,
)
from orca.runtime.sqlite_incident_store import SqliteIncidentStore
from orca.runtime.system_event_bus import SystemEventBus


def _incident(incident_id: str = "x") -> SystemIncident:
    return SystemIncident(
        id=incident_id,
        timestamp=0.0,
        category=IncidentCategory.OTHER,
        severity=IncidentSeverity.INFO,
        execution_id=None,
        thread_id=None,
        message="m",
        detail=OtherIncidentDetail(message_extra=""),
        recovery_action=RecoveryAction.NONE,
        acknowledged=False,
    )


def _incident_event(incident: SystemIncident) -> RuntimeEvent:
    """Mirror SystemRuntime._emit_incident_event for the seam under test."""
    return RuntimeEvent(
        event_name=f"INCIDENT.{incident.id}.{incident.category.value}",
        execution_id=incident.execution_id or "SYSTEM",
        timestamp=incident.timestamp,
        entity_type="INCIDENT",
        entity_id=incident.id,
        status=incident.category.value,
        context=IncidentContext(
            incident_id=incident.id,
            category=incident.category.value,
            severity=incident.severity.value,
            message=incident.message,
            recovery_action=incident.recovery_action.value,
            execution_id=incident.execution_id,
            thread_id=incident.thread_id,
        ),
    )


def _record_other(service: IncidentService) -> SystemIncident:
    return service.record(
        category=IncidentCategory.OTHER,
        severity=IncidentSeverity.INFO,
        message="from a driver-callback thread",
        detail=OtherIncidentDetail(message_extra="off-loop"),
        recovery_action=RecoveryAction.NONE,
    )


async def test_record_off_loop_emits_and_persists() -> None:
    service = IncidentService(SqliteIncidentStore(create_memory_engine()))

    bus = SystemEventBus()
    collected: list[RuntimeEvent] = []
    bus.subscribe(collected.append)  # off-loop-safe sink: list.append
    service.on_record = lambda inc: bus.emit(_incident_event(inc))

    loop = asyncio.get_running_loop()
    inc = await loop.run_in_executor(None, lambda: _record_other(service))

    # Emitted at record-time, off-loop, without raising.
    assert [e.entity_id for e in collected] == [inc.id]
    # Read-your-writes: the read flushes the queued insert to SQLite.
    assert [i.id for i in await service.list()] == [inc.id]


async def test_background_drain_persists_off_loop_record() -> None:
    store = SqliteIncidentStore(create_memory_engine())
    service = IncidentService(store)
    service.start_drain_task()
    try:
        loop = asyncio.get_running_loop()
        inc = await loop.run_in_executor(None, lambda: _record_other(service))
        # The off-loop record signals the drain (call_soon_threadsafe); poll the
        # cheap queue (not the DB) until it consumes the record on its own.
        for _ in range(500):
            if service._pending.empty():
                break
            await asyncio.sleep(0.005)
        assert service._pending.empty(), "background drain did not consume the record"
        await service.stop_drain_task()
        assert [i.id for i in await store.fetch()] == [inc.id]
    finally:
        if service._drain_task is not None:
            await service.stop_drain_task()


async def test_stop_drain_persists_records_queued_without_a_running_drain() -> None:
    """stop_drain_task drains to empty, so a record enqueued before/at teardown
    (no drain ever ran) still reaches the store rather than being stranded."""
    store = SqliteIncidentStore(create_memory_engine())
    service = IncidentService(store)
    inc = _record_other(service)  # enqueue only; no drain running
    assert not service._pending.empty()
    await service.stop_drain_task()
    assert [i.id for i in await store.fetch()] == [inc.id]


async def test_sse_sink_off_loop_emit_wakes_an_awaiting_consumer() -> None:
    """An off-loop incident emit is delivered through a real SseEventSink to an
    awaiting consumer (the deliver_on_loop marshaling path works end-to-end).
    This guards delivery/wiring; the idle-loop lost-wakeup that the old inline
    put_nowait causes needs the loop blocked in select, which the test's own
    awaits prevent, so it is prevented in production by call_soon_threadsafe,
    not asserted here."""
    bus = SystemEventBus()
    sink = SseEventSink()
    sub = sink.subscribe()  # captures the daemon loop
    bus.subscribe(sink.on_event)

    consumer = asyncio.create_task(sub.queue.get())
    await asyncio.sleep(0)  # let the consumer reach `await queue.get()`

    ev = _incident_event(_incident("i0"))
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: bus.emit(ev))  # off-loop emit

    got = await asyncio.wait_for(consumer, timeout=2.0)
    assert got.entity_id == "i0"
    sub.unsubscribe()


def test_emit_snapshots_listeners_so_dispatch_time_subscribes_miss_this_event() -> None:
    """emit() snapshots the listener set under the lock and dispatches the copy,
    so a listener subscribed DURING dispatch does not receive the in-flight
    event. Live-list iteration (the pre-lock bug) would deliver it to the
    just-appended listener, so this fails deterministically on a regression."""
    bus = SystemEventBus()
    late_got: list[RuntimeEvent] = []

    def adder(_e: RuntimeEvent) -> None:
        bus.subscribe(late_got.append)

    bus.subscribe(adder)

    bus.emit(_incident_event(_incident("e1")))
    assert late_got == []  # snapshot: the late listener missed e1

    bus.emit(_incident_event(_incident("e2")))
    assert [e.entity_id for e in late_got] == ["e2"]


async def test_runtime_leaves_an_injected_incident_service_drain_running() -> None:
    """An injected service is process-scoped (a hosted deployment starts/stops it across
    rebuilds); the runtime must not own or stop its drain on shutdown."""
    from unittest.mock import MagicMock

    from orca.runtime.system_runtime import SystemRuntime

    service = IncidentService(SqliteIncidentStore(create_memory_engine()))
    await service.ensure_schema()
    service.start_drain_task()  # the injector owns start/stop

    runtime = SystemRuntime(MagicMock(), incident_service=service)
    assert runtime._owns_incident_service is False

    await runtime.shutdown()
    assert service._drain_task is not None and not service._drain_task.done()

    await service.stop_drain_task()  # the injector cleans up


def test_record_after_the_drain_loop_closed_does_not_raise() -> None:
    """A driver callback can land after the runtime's loop is gone.

    The drain is woken with ``call_soon_threadsafe``, which raises once the
    loop is closed. ``record`` is synchronous and is called from driver
    callback threads, so a wake that raises would surface as a driver failure
    for an incident that was only being written down.
    """
    service = IncidentService(SqliteIncidentStore(create_memory_engine()))

    async def start_the_drain() -> None:
        service.start_drain_task()

    asyncio.run(start_the_drain())

    _record_other(service)
