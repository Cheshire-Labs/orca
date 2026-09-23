"""Recoverable-timeout policy + operator-decision flow, owned by the engine.

A device command that exceeds its advertised ``max_seconds`` without returning
is not a failure: the operator decides whether to wait longer (extend), give up
(abort), or assert the device finished out-of-band (mark complete). That is an
execution concept, so the engine owns it -- not the transport.

``RecoverableTimeoutCoordinator.run_with_timeout`` wraps a single device call:
it races the call against the timeout, and on expiry declares a
RECOVERABLE_TIMEOUT incident, pauses the execution, and parks the dispatch on an
``asyncio.Event`` until an operator decision arrives via ``extend`` / ``abort``
/ ``mark_complete`` (reached from the daemon REST routes, the CLI, or a hosted REST surface).

The coordinator reaches the dispatcher through the
``recoverable_timeout_coordinator`` ContextVar, seeded per execution thread next
to ``current_run_mode``. When unset (unit tests, direct dispatch with no
runtime) the dispatcher runs the call with no timeout -- the prior behavior.

Cancelling the held call on abort/mark_complete stops orca awaiting it; for a
REMOTE driver the physical command keeps running until the wire-cancel contract
propagates the cancellation to orca-client.
"""

import asyncio
import contextlib
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, Protocol, TypeVar

from orca.resource_models.device_error import CommandTimeoutAbortedError
from orca.runtime.incident_store import RecoverableTimeoutContext, SystemIncident
from orca.system.reservation_manager.errors import IRecoverableTimeoutCoordinator


# Bound for a workflow device command whose driver advertises no per-command
# duration (no ``@command_timing``). Single source for both the engine's
# recoverable-timeout fallback and the gateway clock's ad-hoc fallback so a
# command without metadata is bounded identically on either path.
DEFAULT_COMMAND_TIMEOUT_SECONDS: float = 600.0

_T = TypeVar("_T")


class RecoverableDecisionKind(Enum):
    EXTEND = "extend"
    ABORT = "abort"
    MARK_COMPLETE = "mark_complete"


class RecoverableTimeoutNotHeldError(KeyError):
    """No held dispatch matches the given incident id (already resolved, or never timed out)."""


@dataclass
class _HeldDispatch:
    """A device call parked awaiting an operator decision after timing out."""

    incident_id: str
    event: asyncio.Event
    kind: Optional[RecoverableDecisionKind] = None
    additional_seconds: float = 0.0
    operator: str = ""
    reason: str = ""


class IRecoverableTimeoutHost(Protocol):
    """The engine surface the coordinator drives. Implemented by SystemRuntime."""

    def declare_recoverable_timeout(
        self, execution_id: str, context: RecoverableTimeoutContext,
    ) -> SystemIncident: ...

    def resume_all_threads(self, execution_id: str) -> dict[str, int]: ...

    def acknowledge_incident(self, incident_id: str) -> bool: ...

    async def clear_device_fault(self, device_id: str) -> None: ...


async def _await_result_or_deadline(task: asyncio.Future[_T], timeout: float) -> _T:
    """Await ``task`` up to ``timeout`` seconds, raising TimeoutError, without cancelling it.

    Avoids ``wait_for(shield(task), ...)``, whose cancellation-propagation race
    (CPython gh-87555) can drop the timeout under a CPU-starved event loop and
    park the call forever. Routes the deadline and the task's completion through
    one waiter, then decides from ``task.done()`` -- no cancellation to swallow.
    """
    loop = asyncio.get_running_loop()
    waiter: asyncio.Future[None] = loop.create_future()

    def _wake() -> None:
        if not waiter.done():
            waiter.set_result(None)

    def _on_done(_completed: asyncio.Future[_T]) -> None:
        _wake()

    timer = loop.call_later(timeout, _wake)
    task.add_done_callback(_on_done)
    try:
        await waiter
    finally:
        timer.cancel()
        task.remove_done_callback(_on_done)
    if task.done():
        return task.result()
    raise asyncio.TimeoutError()


class RecoverableTimeoutCoordinator:
    """Owns the timeout race + the held-dispatch registry keyed by incident id."""

    def __init__(self, host: IRecoverableTimeoutHost) -> None:
        self._host = host
        self._held: dict[str, _HeldDispatch] = {}

    async def run_with_timeout(
        self,
        execution_id: str,
        device_id: str,
        command: str,
        max_seconds: float,
        coro_factory: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Run one device call; on timeout, declare + pause + await a decision.

        Returns the call result (or a synthetic ``None`` on mark-complete);
        raises ``CommandTimeoutAbortedError`` on abort, or whatever the call
        itself raised.
        """
        command_id = str(uuid.uuid4())
        started = time.monotonic()
        task: asyncio.Task[Any] = asyncio.ensure_future(coro_factory())
        remaining = max_seconds
        timed_out = False
        resumed = False

        def _resume_once() -> None:
            nonlocal resumed
            if not resumed:
                resumed = True
                # Best-effort: the execution may have been removed between the
                # timeout and the operator's decision.
                try:
                    self._host.resume_all_threads(execution_id)
                except KeyError:
                    pass

        try:
            while True:
                try:
                    result = await _await_result_or_deadline(task, remaining)
                except asyncio.TimeoutError:
                    timed_out = True
                    decision = await self._await_decision(
                        execution_id, device_id, command, command_id,
                        elapsed_seconds=time.monotonic() - started,
                        max_seconds=max_seconds,
                    )
                    if decision.kind is RecoverableDecisionKind.EXTEND:
                        remaining = decision.additional_seconds
                        continue
                    _resume_once()
                    if decision.kind is RecoverableDecisionKind.ABORT:
                        await self._cancel_and_drain(task)
                        raise CommandTimeoutAbortedError(
                            f"command '{command}' aborted by operator "
                            f"{decision.operator!r} after recoverable timeout: "
                            f"{decision.reason}",
                            device_name=device_id,
                        )
                    # MARK_COMPLETE: operator asserts the device finished.
                    # Draining cancels the held dispatch, and the controller
                    # faults a device on a cancel because it cannot tell what
                    # the motion did. Here someone looked and can, so their
                    # answer clears it.
                    await self._cancel_and_drain(task)
                    await self._host.clear_device_fault(device_id)
                    return None
                else:
                    # Command resolved on its own (possibly after an extension);
                    # if it had timed out, the paused siblings resume now.
                    if timed_out:
                        _resume_once()
                    return result
        finally:
            if not task.done():
                task.cancel()
            # Covers the command raising its own error after a timeout: the
            # exception propagates, but the paused siblings must still resume.
            if timed_out:
                _resume_once()

    @staticmethod
    async def _cancel_and_drain(task: "asyncio.Task[Any]") -> None:
        """Cancel a held dispatch and await its unwind.

        Awaiting lets the CancelledError propagate through the dispatch (the
        gateway controller catches it to send a wire cancel) and retrieves the
        task's outcome so no 'exception never retrieved' warning leaks. The
        operator decision is authoritative, so whatever the unwinding dispatch
        raises is discarded.
        """
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _await_decision(
        self,
        execution_id: str,
        device_id: str,
        command: str,
        command_id: str,
        elapsed_seconds: float,
        max_seconds: float,
    ) -> _HeldDispatch:
        context = RecoverableTimeoutContext(
            device_id=device_id,
            command=command,
            command_id=command_id,
            elapsed_seconds=elapsed_seconds,
            max_seconds=max_seconds,
        )
        incident = self._host.declare_recoverable_timeout(execution_id, context)
        held = _HeldDispatch(incident_id=incident.id, event=asyncio.Event())
        self._held[incident.id] = held
        try:
            await held.event.wait()
        finally:
            self._held.pop(incident.id, None)
        return held

    def _resolve(
        self, incident_id: str, kind: RecoverableDecisionKind,
    ) -> _HeldDispatch:
        held = self._held.get(incident_id)
        if held is None:
            raise RecoverableTimeoutNotHeldError(
                f"no held command for recoverable-timeout incident {incident_id!r}"
            )
        held.kind = kind
        self._host.acknowledge_incident(incident_id)
        return held

    def extend(self, incident_id: str, additional_seconds: float) -> None:
        """Grant the held command more time; re-arms the timer.

        ``additional_seconds`` must be positive: a value <= 0 would re-arm the
        timer with a non-positive timeout, fire immediately, and re-declare the
        incident in a loop. Rejected here so every caller (daemon, a hosted
        deployment, tests) is protected by the engine invariant.
        """
        if additional_seconds <= 0:
            raise ValueError(
                f"additional_seconds must be positive, got {additional_seconds}"
            )
        held = self._resolve(incident_id, RecoverableDecisionKind.EXTEND)
        held.additional_seconds = additional_seconds
        held.event.set()

    def abort(self, incident_id: str, operator: str, reason: str) -> None:
        """Fail the held command; the workflow's failure policy fires."""
        held = self._resolve(incident_id, RecoverableDecisionKind.ABORT)
        held.operator = operator
        held.reason = reason
        held.event.set()

    def mark_complete(self, incident_id: str, operator: str, reason: str) -> None:
        """Operator asserts the device finished; synthesize success, resume."""
        held = self._resolve(incident_id, RecoverableDecisionKind.MARK_COMPLETE)
        held.operator = operator
        held.reason = reason
        held.event.set()


recoverable_timeout_coordinator: ContextVar[
    Optional[IRecoverableTimeoutCoordinator]
] = ContextVar("recoverable_timeout_coordinator", default=None)
