"""Each write to the labware record sends one LABWARE event, whichever route made it, so a stale read knows to refresh."""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from orca.events.execution_context import LabwareRecordContext
from orca.runtime.errors import BarcodeInUseError
from orca.runtime.server_events import ServerEvents
from orca.runtime.sinks import CollectorSink
from orca.runtime.system_runtime import SystemRuntime
from tests.memory_activity import announced_labware_service, memory_runtime
from tests.test_system_runtime import _build_simple_system


@pytest_asyncio.fixture
async def watched() -> AsyncIterator[tuple[SystemRuntime, CollectorSink]]:
    sink = CollectorSink()
    events = ServerEvents()
    events.add_sink(sink)
    system, _ = await _build_simple_system(labware_service=announced_labware_service(events))
    runtime = await memory_runtime(system)
    await runtime.start()
    try:
        yield runtime, sink
    finally:
        await runtime.shutdown(confirm=True)


def _labware_events(sink: CollectorSink) -> list[tuple[str, str | None]]:
    return [
        (e.status, e.context.position_id)
        for e in sink.events if e.entity_type == "LABWARE" and isinstance(e.context, LabwareRecordContext)
    ]


async def test_a_registered_plate_is_saved_and_then_moved_to_its_site(
    watched: tuple[SystemRuntime, CollectorSink],
) -> None:
    runtime, sink = watched

    await runtime.labware.register("plate_96", location="pad1", confirm=True)

    assert ("SAVED", None) in _labware_events(sink)
    assert ("MOVED", "pad1") in _labware_events(sink)


async def test_a_barcode_edit_and_a_discharge_are_each_announced(
    watched: tuple[SystemRuntime, CollectorSink],
) -> None:
    runtime, sink = watched
    plate = await runtime.labware.register("plate_96", location="pad1", confirm=True)
    sink.clear()

    await runtime.labware.edit_barcode(plate.id, "BC-9", confirm=True)
    await runtime.labware.discharge_labware(plate.id, confirm=True)

    statuses = [status for status, _ in _labware_events(sink)]
    assert statuses[0] == "SAVED"
    assert "CLEARED" in statuses


async def test_a_refused_write_sends_nothing(watched: tuple[SystemRuntime, CollectorSink]) -> None:
    runtime, sink = watched
    await runtime.labware.register("plate_96", barcode="BC-1", location="pad1", confirm=True)
    sink.clear()

    with pytest.raises(BarcodeInUseError):
        await runtime.labware.register("plate_96", barcode="BC-1", location="pad2", confirm=True)

    assert _labware_events(sink) == []
