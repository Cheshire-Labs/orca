"""Log what this process holds when a SQLite statement fails with 'database is locked'."""

import asyncio
import logging
import sys
import threading
import time
import traceback
import types
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from types import FrameType
from typing import Protocol, TypeVar

from sqlalchemy import Engine, event
from sqlalchemy.engine import ExceptionContext
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import ConnectionPoolEntry, PoolProxiedConnection

logger = logging.getLogger(__name__)

_HOLDER_STACK_LIMIT = 30
_THREAD_STACK_LIMIT = 12
_TASK_LIST_LIMIT = 200


class _TransactionFlag(Protocol):
    @property
    def in_transaction(self) -> bool: ...


_Result = TypeVar("_Result")


@dataclass(frozen=True)
class _Holder:
    thread_id: int
    thread_name: str
    task_frames: Callable[[], list[FrameType]] | None
    task_name: str | None
    loop: asyncio.AbstractEventLoop | None
    since: float


@dataclass
class _Connection:
    database: str
    holder: _Holder | None = None


# Every pooled SQLite connection in this process, across every engine, so a report on one
# engine also sees the other engines on the same file. Weak, so an undisposed engine still closes.
_connections: weakref.WeakKeyDictionary[ConnectionPoolEntry, _Connection] = weakref.WeakKeyDictionary()
_connections_lock = threading.Lock()


def attach_lock_report(engine: Engine) -> None:
    """Log every connection on the same file, with who checked it out and their stack, when a statement is locked out."""
    database = engine.url.database or ""

    @event.listens_for(engine, "connect")
    def _connected(_dbapi: DBAPIConnection, record: ConnectionPoolEntry) -> None:
        with _connections_lock:
            _connections[record] = _Connection(database)

    @event.listens_for(engine, "checkout")
    def _checked_out(
        _dbapi: DBAPIConnection, record: ConnectionPoolEntry, _proxy: PoolProxiedConnection,
    ) -> None:
        entry = _connections.get(record)
        if entry is not None:
            entry.holder = _current_holder()

    @event.listens_for(engine, "checkin")
    def _checked_in(_dbapi: DBAPIConnection | None, record: ConnectionPoolEntry) -> None:
        entry = _connections.get(record)
        if entry is not None:
            entry.holder = None

    @event.listens_for(engine, "close")
    def _closed(_dbapi: DBAPIConnection, record: ConnectionPoolEntry) -> None:
        with _connections_lock:
            _connections.pop(record, None)

    @event.listens_for(engine, "detach")
    def _detached(_dbapi: DBAPIConnection, record: ConnectionPoolEntry) -> None:
        with _connections_lock:
            _connections.pop(record, None)

    @event.listens_for(engine, "handle_error")
    def _locked_out(context: ExceptionContext) -> None:
        if "database is locked" not in str(context.original_exception):
            return
        try:
            logger.error(_report(database, context))
        except Exception:
            logger.exception("Could not build the report for a 'database is locked' statement on %s", database)


def _current_holder() -> _Holder:
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    task = asyncio.current_task() if loop is not None else None
    return _Holder(
        thread_id=threading.get_ident(),
        thread_name=threading.current_thread().name,
        task_frames=_frames_of(weakref.ref(task)) if task is not None else None,
        task_name=task.get_name() if task is not None else None,
        loop=loop,
        since=time.monotonic(),
    )


def _frames_of(task_ref: weakref.ref[asyncio.Task[_Result]]) -> Callable[[], list[FrameType]]:
    def frames() -> list[FrameType]:
        task = task_ref()
        return _await_chain(task) if task is not None else []

    return frames


def _await_chain(task: asyncio.Task[_Result]) -> list[FrameType]:
    """The task's frames from its own coroutine inward; Task.get_stack gives only the outermost one."""
    frames: list[FrameType] = []
    step = task.get_coro()
    while len(frames) < _HOLDER_STACK_LIMIT:
        if isinstance(step, types.CoroutineType) and step.cr_frame is not None:
            frames.append(step.cr_frame)
            step = step.cr_await
        elif isinstance(step, types.GeneratorType) and step.gi_frame is not None:
            frames.append(step.gi_frame)
            step = step.gi_yieldfrom
        else:
            break
    return frames


def _report(database: str, context: ExceptionContext) -> str:
    failed = _failed_connection(context)
    now = time.monotonic()
    statement = f": {context.statement}" if context.statement else ""
    lines = [
        f"A statement on {database} failed with 'database is locked'{statement}",
        "Connections this process has open on that file ('checked out by' is who took it from the pool):",
    ]
    idle_in_transaction = 0
    loops: set[asyncio.AbstractEventLoop] = set()
    with _connections_lock:
        snapshot = [(record, entry) for record, entry in _connections.items() if entry.database == database]
    for record, entry in snapshot:
        try:
            line, idle = _describe_connection(record, entry.holder, failed, now)
        except Exception as exc:
            line, idle = f"- connection: could not be read ({exc!r})", False
        lines.append(line)
        idle_in_transaction += idle
        if entry.holder is not None and entry.holder.loop is not None:
            loops.add(entry.holder.loop)
    if idle_in_transaction == 0:
        lines.append(
            "No connection in this process is idle in a transaction on this file. Another process, "
            "or a connection Orca did not open, most likely holds the lock."
        )
    lines.extend(_all_tasks(loops))
    lines.extend(_all_threads())
    return "\n".join(lines)


def _failed_connection(context: ExceptionContext) -> _TransactionFlag | None:
    if context.connection is None:
        return None
    driver: _TransactionFlag | None = context.connection.connection.driver_connection
    return driver


def _describe_connection(
    record: ConnectionPoolEntry, holder: _Holder | None, failed: _TransactionFlag | None, now: float,
) -> tuple[str, bool]:
    """One report line for a connection, and whether it is idle in a transaction (the likely holder)."""
    driver: _TransactionFlag | None = record.driver_connection
    in_transaction = driver is not None and driver.in_transaction
    if driver is not None and driver is failed:
        return "- the connection whose statement failed", False
    if holder is None:
        state = "in a transaction" if in_transaction else "not in a transaction"
        return f"- connection: {state}, not checked out", in_transaction
    frames = _holder_frames(holder)
    running = bool(frames) and _is_inside_a_statement(frames[-1])
    if not in_transaction:
        state = "not in a transaction"
    elif running:
        state = "in a transaction and running a statement (likely waiting for the lock)"
    else:
        state = "in a transaction and idle (likely holding the lock)"
    task = f"task '{holder.task_name}' on " if holder.task_name is not None else ""
    text = f"- connection: {state}, checked out by {task}thread '{holder.thread_name}' {now - holder.since:.1f} s ago"
    return text + "\n" + _indent(_format_frames(frames)), in_transaction and not running


def _holder_frames(holder: _Holder) -> list[FrameType]:
    if holder.task_frames is not None:
        return holder.task_frames()
    frame = sys._current_frames().get(holder.thread_id)
    return _thread_frames(frame, _HOLDER_STACK_LIMIT) if frame is not None else []


def _thread_frames(innermost: FrameType, limit: int) -> list[FrameType]:
    frames: list[FrameType] = []
    frame: FrameType | None = innermost
    while frame is not None and len(frames) < limit:
        frames.append(frame)
        frame = frame.f_back
    frames.reverse()
    return frames


def _is_inside_a_statement(frame: FrameType) -> bool:
    filename = frame.f_code.co_filename.replace("\\", "/")
    return "/sqlalchemy/" in filename or "/aiosqlite/" in filename


def _format_frames(frames: list[FrameType]) -> str:
    if not frames:
        return "(no stack: the holder has ended)"
    summary = traceback.StackSummary.extract((frame, frame.f_lineno) for frame in frames)
    return "".join(summary.format())


def _all_tasks(loops: set[asyncio.AbstractEventLoop]) -> list[str]:
    lines: list[str] = []
    for loop in loops:
        tasks = list(asyncio.all_tasks(loop))
        lines.append(f"All {len(tasks)} asyncio tasks on the loop, with where each is waiting:")
        for task in tasks[:_TASK_LIST_LIMIT]:
            frames = _await_chain(task)
            where = f" at {frames[-1].f_code.co_filename}:{frames[-1].f_lineno}" if frames else ""
            lines.append(f"- task '{task.get_name()}'{where}")
        if len(tasks) > _TASK_LIST_LIMIT:
            lines.append(f"- and {len(tasks) - _TASK_LIST_LIMIT} more")
    return lines


def _all_threads() -> list[str]:
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    lines = [f"All threads, innermost {_THREAD_STACK_LIMIT} frames each:"]
    for ident, frame in sys._current_frames().items():
        stack = _format_frames(_thread_frames(frame, _THREAD_STACK_LIMIT))
        lines.append(f"- thread '{names.get(ident, ident)}'\n" + _indent(stack))
    return lines


def _indent(text: str) -> str:
    return "\n".join("    " + line for line in text.rstrip().splitlines())
