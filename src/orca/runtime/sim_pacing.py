"""Pacing for simulated runs.

A simulated step lasts its real-world duration times the run's ``sim_time_scale``:
1 is real time, 0.1 is ten times faster, 0 is no pacing and the default. The scale
belongs to the execution and can change while it runs; every paced wait wakes on a
change and finishes at the new scale.

A change wakes a wait through a future the wait took out before it parked, not
through an Event pulse: a pulse landing in the loop turn before the wait's own
racer starts would be missed, and that step would finish at the old scale.

LIVE runs never reach this module. A thread seeds ``current_sim_pacer`` with None
when its run mode is LIVE, and every pacing site returns at once on None.
"""

import asyncio
import inspect
import math
import time
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence

from orca.async_util import drain_cancelled_waiters
from orca.runtime.run_modes import WorkflowRunMode

NOMINAL_PLATE_MOVE_SECONDS = 10.0
"""A transporter pick, travel and place."""

DEFAULT_NOMINAL_STEP_SECONDS = 5.0
"""A device call that declares no duration and whose interface carries no timing."""


def validate_time_scale(scale: float) -> float:
    if (
        isinstance(scale, bool)
        or not isinstance(scale, (int, float))
        or not math.isfinite(scale)
        or scale < 0
    ):
        raise ValueError(
            "sim_time_scale must be a finite number >= 0 "
            f"(1 is real time, 0 is no pacing); got {scale!r}"
        )
    return float(scale)


class PacingOutcome(str, Enum):
    ELAPSED = "elapsed"
    PAUSE_REQUESTED = "pause_requested"


@dataclass(frozen=True)
class PacingResult:
    outcome: PacingOutcome
    remaining_sim_seconds: float


class SimPacer:
    """One per simulated execution: the scale in force and the waits it governs."""

    def __init__(
        self,
        time_scale: float,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._time_scale = validate_time_scale(time_scale)
        self._change_waiters: list[asyncio.Future[None]] = []
        self._sleep = sleep
        self._waits = 0

    @property
    def waiting(self) -> bool:
        """True while at least one paced step is parked on this pacer."""
        return self._waits > 0

    @property
    def time_scale(self) -> float:
        return self._time_scale

    def set_time_scale(self, scale: float) -> None:
        self._time_scale = validate_time_scale(scale)
        waiters, self._change_waiters = self._change_waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    def _a_change_waiter(self) -> asyncio.Future[None]:
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._change_waiters.append(waiter)
        return waiter

    def remaining_after(self, nominal_sim_seconds: float, started_at: float) -> float:
        """Simulated seconds of a step still owed once the real call has used
        the wall time since ``started_at``, counted at the current scale."""
        scale = self._time_scale
        if scale == 0:
            return 0.0
        elapsed_sim = (time.monotonic() - started_at) / scale
        return max(0.0, nominal_sim_seconds - elapsed_sim)

    async def pace(
        self,
        sim_seconds: float,
        *,
        pause_requested: asyncio.Event | None = None,
    ) -> PacingResult:
        """Wait out ``sim_seconds`` of simulated time at the scale in force.

        Ends early when the scale changes (then continues at the new one) or
        when ``pause_requested`` is set (then returns what is still owed, so the
        caller can hold and come back). A cancelled task propagates as usual.
        """
        remaining = sim_seconds
        while remaining > 0:
            scale = self._time_scale
            if scale == 0:
                return PacingResult(PacingOutcome.ELAPSED, 0.0)
            if pause_requested is not None and pause_requested.is_set():
                return PacingResult(PacingOutcome.PAUSE_REQUESTED, remaining)
            started = time.monotonic()
            changed = self._a_change_waiter()
            sleeper = asyncio.ensure_future(self._sleep(remaining * scale))
            waiters: list[asyncio.Future[None] | asyncio.Future[Literal[True]]] = [
                sleeper, changed,
            ]
            if pause_requested is not None:
                waiters.append(asyncio.ensure_future(pause_requested.wait()))
            self._waits += 1
            try:
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            finally:
                self._waits -= 1
                if changed in self._change_waiters:
                    self._change_waiters.remove(changed)
                await drain_cancelled_waiters(*waiters)
            if sleeper.done() and not sleeper.cancelled():
                return PacingResult(PacingOutcome.ELAPSED, 0.0)
            remaining -= (time.monotonic() - started) / scale
        return PacingResult(PacingOutcome.ELAPSED, 0.0)


def _as_seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def declared_duration(
    method: Callable[..., Any],
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
) -> float | None:
    """The real-world seconds the call itself declares: a ``duration`` argument,
    or a ``duration`` field on a request object it passes."""
    try:
        bound = inspect.signature(method).bind_partial(*args, **kwargs)
    except (TypeError, ValueError):
        bound = None
    if bound is not None:
        seconds = _as_seconds(bound.arguments.get("duration"))
        if seconds is not None:
            return seconds
    for value in (*args, *kwargs.values()):
        seconds = _as_seconds(getattr(value, "duration", None))
        if seconds is not None:
            return seconds
    return None


def nominal_step_seconds(
    method: Callable[..., Any],
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
    *,
    typical_seconds: float | None,
) -> float:
    """Real-world seconds a simulated device call stands for.

    The call's own ``duration`` wins. Otherwise the driver interface's
    ``@command_timing(typical=...)`` for the command, which the engine already
    trusts for timeouts. Otherwise ``DEFAULT_NOMINAL_STEP_SECONDS``.
    """
    declared = declared_duration(method, args, kwargs)
    if declared is not None:
        return declared
    if typical_seconds is not None:
        return typical_seconds
    return DEFAULT_NOMINAL_STEP_SECONDS


def pacer_for_run_mode(
    run_mode: WorkflowRunMode, pacer: SimPacer | None,
) -> SimPacer | None:
    """What a thread seeds: None for LIVE, which the hardware paces; else the run's pacer."""
    return None if run_mode is WorkflowRunMode.LIVE else pacer


# Seeded at thread start next to `current_run_mode`: the run's pacer, or None for
# a LIVE thread. Unset (unit tests, direct dispatch) reads None: nothing paces by accident.
current_sim_pacer: ContextVar[SimPacer | None] = ContextVar(
    "current_sim_pacer", default=None,
)
