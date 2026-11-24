"""An in-memory IAuditStore with the real AuditWriter in front of it, for reading audit rows in tests."""

import asyncio
import logging
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from orca.runtime.audit_store import AUDIT_LOGGER_NAME, AuditEntry, AuditQuery, IAuditStore
from orca.runtime.log_store_writer import AuditWriter
from tests.unsaved_records import UnsavedRecords


class InMemoryAuditStore(IAuditStore):
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def append(self, entry: AuditEntry) -> None:
        self.entries.append(entry)

    async def newest_first(self, query: AuditQuery) -> list[AuditEntry]:
        matching = [
            e for e in reversed(self.entries)
            if (query.action_name is None or e.action_name == query.action_name)
            and (query.danger_level is None or e.danger_level == query.danger_level)
            and (query.since is None or e.timestamp >= query.since)
            and (query.until is None or e.timestamp <= query.until)
        ]
        return matching[:query.limit]


class AuditCapture:
    """Reads what the writer stored, through the store interface, oldest first."""

    def __init__(
        self, store: IAuditStore, writer: AuditWriter, loop: asyncio.AbstractEventLoop, unsaved: UnsavedRecords,
    ) -> None:
        self._store = store
        self._writer = writer
        self._loop = loop
        self.unsaved = unsaved

    def rows(self) -> list[AuditEntry]:
        self._writer.wait_until_written()
        newest = asyncio.run_coroutine_threadsafe(
            self._store.newest_first(AuditQuery(limit=10_000)), self._loop,
        ).result()
        return list(reversed(newest))


@contextmanager
def capture_audit(
    store: IAuditStore | None = None, retry_delays: Sequence[float] = (),
) -> Iterator[AuditCapture]:
    """Install a writer on its own loop thread, so sync and async tests read it the same way."""
    logger = logging.getLogger(AUDIT_LOGGER_NAME)
    level = logger.level
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    store = store or InMemoryAuditStore()
    unsaved = UnsavedRecords()
    writer = AuditWriter(store, loop, unsaved, retry_delays)
    writer.start()
    try:
        yield AuditCapture(store, writer, loop, unsaved)
    finally:
        asyncio.run_coroutine_threadsafe(writer.stop(), loop).result()
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()
        logger.setLevel(level)
