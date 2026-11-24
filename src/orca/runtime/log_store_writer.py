"""Stores what is logged to `orca.audit` and `orca.events`, off the thread that logged it.

Each writer puts a `QueueHandler` on its logger. A listener thread hands each
record to a store on the server's event loop, so logging never waits on the
database. Any other handler on the same logger can send the records elsewhere.

A record the store refuses is tried again, then handed to the unsaved-record
reporter on the server's loop. So is a record that arrives at a full queue.
"""

import asyncio
import logging
import queue
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine, Sequence
from typing import Generic, TypeVar

from pydantic import BaseModel
from logging.handlers import QueueHandler, QueueListener

from orca.events.runtime_event import RuntimeEvent
from orca.runtime.audit_store import AUDIT_LOGGER_NAME, AuditEntry, IAuditStore, audit_entry_of
from orca.runtime.event_store import EVENT_LOGGER_NAME, IEventStore, event_of
from orca.runtime.incident_store import IncidentCategory
from orca.runtime.unsaved_record import RETRY_DELAYS_SECONDS, IUnsavedRecordReporter, UnsavedRecord
from orca.state.ops_store import SYSTEM_ID

_logger = logging.getLogger(__name__)

QUEUE_SIZE = 10_000

T = TypeVar("T", bound=BaseModel)


class DropCountingHandler(logging.Handler, ABC):
    """A handler that counts the records it gave up on."""

    def __init__(self) -> None:
        super().__init__()
        self.dropped = 0

    @abstractmethod
    def give_up_on(self, record: logging.LogRecord, error: Exception) -> None:
        """Report a record that was never tried. Safe from any thread."""


class StoreHandler(DropCountingHandler, Generic[T]):
    """Writes one record to a store, awaiting it on the server's loop.

    A failed write is tried again after each of `retry_delays`, then handed to
    `unsaved_records`; it never reaches the code that logged.
    """

    def __init__(
        self, loop: asyncio.AbstractEventLoop, unsaved_records: IUnsavedRecordReporter, retry_delays: Sequence[float],
    ) -> None:
        super().__init__()
        self._loop = loop
        self._unsaved_records = unsaved_records
        self._retry_delays = tuple(retry_delays)

    @abstractmethod
    def _payload(self, record: logging.LogRecord) -> T | None:
        """What `record` carries to store, or None."""

    @abstractmethod
    def _write(self, payload: T) -> Coroutine[None, None, None]: ...

    @abstractmethod
    def _unsaved(self, payload: T) -> UnsavedRecord: ...

    def emit(self, record: logging.LogRecord) -> None:
        payload = self._payload(record)
        if payload is None:
            _logger.warning("%s record carries nothing to store: %s", record.name, record.getMessage())
            return
        error = self._write_or_error(record.name, payload)
        if error is not None:
            self._give_up(payload, error)

    def give_up_on(self, record: logging.LogRecord, error: Exception) -> None:
        payload = self._payload(record)
        if payload is not None:
            self._give_up(payload, error)

    def _write_or_error(self, name: str, payload: T) -> Exception | None:
        attempts = 1 + len(self._retry_delays)
        for attempt, delay in enumerate((*self._retry_delays, None), start=1):
            try:
                asyncio.run_coroutine_threadsafe(self._write(payload), self._loop).result()
                return None
            except Exception as error:
                _logger.error("%s write failed (attempt %d of %d)", name, attempt, attempts, exc_info=True)
                if delay is None:
                    return error
                time.sleep(delay)
        return None

    def _give_up(self, payload: T, error: Exception) -> None:
        self.dropped += 1
        unsaved = self._unsaved(payload)
        try:
            self._loop.call_soon_threadsafe(self._unsaved_records.report, unsaved, error)
        except RuntimeError:
            # The loop is closed, so nothing can pause a run or raise an incident any more.
            _logger.error("could not save %s: %s", unsaved.kind, unsaved.record, exc_info=error)


class AuditStoreHandler(StoreHandler[AuditEntry]):
    def __init__(
        self, store: IAuditStore, loop: asyncio.AbstractEventLoop, unsaved_records: IUnsavedRecordReporter,
        retry_delays: Sequence[float] = RETRY_DELAYS_SECONDS,
    ) -> None:
        super().__init__(loop, unsaved_records, retry_delays)
        self._store = store

    def _payload(self, record: logging.LogRecord) -> AuditEntry | None:
        return audit_entry_of(record)

    def _write(self, payload: AuditEntry) -> Coroutine[None, None, None]:
        return self._store.append(payload)

    def _unsaved(self, payload: AuditEntry) -> UnsavedRecord:
        execution_id = payload.call_args.get("execution_id")
        return UnsavedRecord(
            kind="an audit row",
            record=payload.model_dump_json(),
            execution_id=execution_id if isinstance(execution_id, str) else None,
        )


class EventStoreHandler(StoreHandler[RuntimeEvent]):
    def __init__(
        self, store: IEventStore, loop: asyncio.AbstractEventLoop, unsaved_records: IUnsavedRecordReporter,
        retry_delays: Sequence[float] = RETRY_DELAYS_SECONDS,
    ) -> None:
        super().__init__(loop, unsaved_records, retry_delays)
        self._store = store

    def _payload(self, record: logging.LogRecord) -> RuntimeEvent | None:
        return event_of(record)

    def _write(self, payload: RuntimeEvent) -> Coroutine[None, None, None]:
        return self._store.append(payload)

    def _unsaved(self, payload: RuntimeEvent) -> UnsavedRecord:
        return UnsavedRecord(
            kind="an event",
            record=payload.model_dump_json(),
            execution_id=None if payload.execution_id == SYSTEM_ID else payload.execution_id,
            reports_an_unsaved_record=(
                payload.entity_type == "INCIDENT" and payload.status == IncidentCategory.RECORD_NOT_SAVED.value
            ),
        )


class RecordQueueFullError(Exception):
    """A record arrived while the writer's queue was full, so it was never tried."""


class _BoundedQueueHandler(QueueHandler):
    """Queues a record, or hands it to `on_full` when the queue is full, so logging never blocks."""

    def __init__(
        self, records: queue.Queue[logging.LogRecord | None],
        on_full: Callable[[logging.LogRecord, Exception], None],
    ) -> None:
        super().__init__(records)
        self._on_full = on_full

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self._on_full(record, RecordQueueFullError(f"the {record.name} queue is full"))


class _Listener(QueueListener):
    def __init__(self, records: queue.Queue[logging.LogRecord | None], handler: logging.Handler) -> None:
        super().__init__(records, handler)
        self._records = records

    def enqueue_sentinel(self) -> None:
        # Waits for room: the queue is bounded, and stopping must still write what is queued.
        # None is QueueListener's stop sentinel.
        self._records.put(None)


class _CountingQueue(queue.Queue[logging.LogRecord | None]):
    """Counts the records handled, so a wait can stop at the ones queued when it began."""

    def __init__(self, maxsize: int) -> None:
        super().__init__(maxsize)
        self.handled = 0

    def task_done(self) -> None:
        # Queue.task_done's own body, counting under the same lock so `handled_when_all_queued_now` never sees half.
        with self.all_tasks_done:
            unfinished = self.unfinished_tasks - 1
            if unfinished < 0:
                raise ValueError("task_done() called too many times")
            if unfinished == 0:
                self.all_tasks_done.notify_all()
            self.unfinished_tasks = unfinished
            self.handled += 1

    def handled_when_all_queued_now(self) -> int:
        with self.mutex:
            return self.handled + self.unfinished_tasks


class LogStoreWriter:
    """Puts a `StoreHandler` behind a bounded `QueueHandler` on one logger."""

    def __init__(self, logger_name: str, handler: DropCountingHandler, queue_size: int = QUEUE_SIZE) -> None:
        self._logger_name = logger_name
        self._queue: _CountingQueue = _CountingQueue(queue_size)
        self._queue_handler = _BoundedQueueHandler(self._queue, handler.give_up_on)
        self._store_handler = handler
        self._listener = _Listener(self._queue, handler)

    @property
    def dropped(self) -> int:
        """Records never stored: the queue was full, or every write attempt failed."""
        return self._store_handler.dropped

    def start(self) -> None:
        logger = logging.getLogger(self._logger_name)
        # The records are INFO; a root logger left at WARNING would drop them before any handler.
        if logger.level == logging.NOTSET or logger.level > logging.INFO:
            logger.setLevel(logging.INFO)
        logger.addHandler(self._queue_handler)
        self._listener.start()

    def wait_until_written(self) -> None:
        """Block until every record logged so far has been handled."""
        self._queue.join()

    async def written_within(self, timeout: float) -> bool:
        """True once every record logged so far is handled; False if ``timeout`` runs out first.

        Records logged after the call do not hold it up. It holds no thread, so a cancelled or timed-out wait
        leaves nothing behind that blocks shutdown.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        handled_by_then = self._queue.handled_when_all_queued_now()
        while self._queue.handled < handled_by_then:
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(_WRITTEN_POLL_S)
        return True

    async def stop(self) -> None:
        """Detach from the logger, then write what is still queued."""
        logging.getLogger(self._logger_name).removeHandler(self._queue_handler)
        # The listener awaits writes on this loop, so stopping it must not block the loop.
        await asyncio.to_thread(self._listener.stop)


_WRITTEN_POLL_S = 0.02


class AuditWriter(LogStoreWriter):
    def __init__(
        self, store: IAuditStore, loop: asyncio.AbstractEventLoop, unsaved_records: IUnsavedRecordReporter,
        retry_delays: Sequence[float] = RETRY_DELAYS_SECONDS, queue_size: int = QUEUE_SIZE,
    ) -> None:
        super().__init__(AUDIT_LOGGER_NAME, AuditStoreHandler(store, loop, unsaved_records, retry_delays), queue_size)


class EventWriter(LogStoreWriter):
    def __init__(
        self, store: IEventStore, loop: asyncio.AbstractEventLoop, unsaved_records: IUnsavedRecordReporter,
        retry_delays: Sequence[float] = RETRY_DELAYS_SECONDS, queue_size: int = QUEUE_SIZE,
    ) -> None:
        super().__init__(EVENT_LOGGER_NAME, EventStoreHandler(store, loop, unsaved_records, retry_delays), queue_size)
