"""The audit writer stores every orca.audit row off the caller's thread, and a failed write never fails the action."""

import asyncio
import logging
import threading

import pytest

from orca.runtime.audit_store import AUDIT_LOGGER_NAME, AuditEntry
from orca.runtime.danger import DangerLevel, dangerous

from orca.runtime.log_store_writer import AuditWriter, RecordQueueFullError
from tests.audit_capture import InMemoryAuditStore, capture_audit
from tests.unsaved_records import UnsavedRecords


@dangerous(name="test.audit_writer.move", level=DangerLevel.OPERATOR, message="move {plate}")
def _move(plate: str, *, reason: str | None = None) -> str:
    del reason
    return f"moved {plate}"


class _HeldStore(InMemoryAuditStore):
    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()

    async def append(self, entry: AuditEntry) -> None:
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        await super().append(entry)


class _FailingStore(InMemoryAuditStore):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.attempts = 0

    async def append(self, entry: AuditEntry) -> None:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise OSError("database is locked")
        await super().append(entry)


def test_the_row_is_stored_with_every_field_the_action_logged() -> None:
    with capture_audit() as audit:
        _move("p1", reason="wrong slot", confirm=True)

        [row] = audit.rows()

    assert (row.action_name, row.danger_level, row.reason, row.call_args) == (
        "test.audit_writer.move", "OPERATOR", "wrong slot", {"plate": "p1"},
    )


def test_the_action_returns_before_the_store_has_written() -> None:
    store = _HeldStore()
    with capture_audit(store) as audit:
        assert _move("p1", confirm=True) == "moved p1"
        assert store.entries == []

        store.release.set()

        assert [r.action_name for r in audit.rows()] == ["test.audit_writer.move"]


def test_stopping_the_writer_stores_what_was_still_queued() -> None:
    store = _HeldStore()
    with capture_audit(store):
        _move("p1", confirm=True)
        store.release.set()

    assert [e.action_name for e in store.entries] == ["test.audit_writer.move"]


def test_a_failed_write_is_logged_at_error_and_retried(caplog: pytest.LogCaptureFixture) -> None:
    store = _FailingStore(failures=2)
    with caplog.at_level(logging.ERROR, logger="orca.runtime.log_store_writer"):
        with capture_audit(store, retry_delays=(0.0, 0.0)) as audit:
            assert _move("p1", confirm=True) == "moved p1"

            [row] = audit.rows()

    assert row.action_name == "test.audit_writer.move"
    assert store.attempts == 3
    failures = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(failures) == 2
    assert "attempt 1 of 3" in failures[0].getMessage()
    assert audit.unsaved.reported == []


def test_a_write_that_never_succeeds_reports_the_whole_row_and_the_action_still_returns() -> None:
    store = _FailingStore(failures=99)
    with capture_audit(store, retry_delays=(0.0, 0.0)) as audit:
        assert _move("p1", reason="lost", confirm=True) == "moved p1"

        assert audit.rows() == []

    assert store.attempts == 3
    [(unsaved, error)] = audit.unsaved.reported
    assert unsaved.kind == "an audit row"
    assert '"reason":"lost"' in unsaved.record and '"plate":"p1"' in unsaved.record
    assert isinstance(error, OSError)


def test_a_record_without_an_entry_is_dropped_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="orca.runtime.log_store_writer"):
        with capture_audit() as audit:
            logging.getLogger(AUDIT_LOGGER_NAME).info("hand-written line")

            assert audit.rows() == []

    assert any("carries nothing to store" in r.getMessage() for r in caplog.records)


def test_stopping_removes_the_writer_from_the_logger() -> None:
    logger = logging.getLogger(AUDIT_LOGGER_NAME)
    before = list(logger.handlers)
    with capture_audit():
        assert len(logger.handlers) == len(before) + 1

    assert logger.handlers == before



def test_a_record_the_full_queue_cannot_take_is_reported() -> None:
    logger = logging.getLogger(AUDIT_LOGGER_NAME)
    level = logger.level
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    store = _HeldStore()
    unsaved = UnsavedRecords()
    writer = AuditWriter(store, loop, unsaved, retry_delays=(), queue_size=1)
    writer.start()
    try:
        for plate in ("p1", "p2", "p3"):
            assert _move(plate, confirm=True) == f"moved {plate}"
    finally:
        store.release.set()
        asyncio.run_coroutine_threadsafe(writer.stop(), loop).result()
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()
        logger.setLevel(level)

    # The writer holds one record and the queue one more, so the third is dropped.
    assert writer.dropped >= 1
    assert len(store.entries) == 3 - writer.dropped
    assert len(unsaved.reported) == writer.dropped
    assert all(isinstance(error, RecordQueueFullError) for _, error in unsaved.reported)
