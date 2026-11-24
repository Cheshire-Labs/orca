"""The speed-up of a simulated run.

A simulated command lasts its real-world duration divided by the run's ``sim_speedup``:
1 is real time, 100 is a hundred times faster, from SIM_SPEEDUP_RANGE. A run with no
speed-up does not wait at all. Each server sets its own default (100 on a local
install); ``off`` sets it to no waiting. The speed-up belongs to the execution and
can change while it runs; a change applies from the next instrument command.

The device's simulator does the waiting. The engine sends each command with the
speed-up in force and the seconds it budgets for it. Bring-up and a command that
moves nothing do not wait: the driver interface marks them ``@simulator_does_not_wait``.

A thread seeds ``current_sim_pacer`` with None when its run mode is not SIM.
"""

import inspect
import math
from contextvars import ContextVar
from typing import Any, Callable, Mapping, Sequence

from orca.modes.run_modes import WorkflowRunMode

DEFAULT_NOMINAL_STEP_SECONDS = 5.0
"""A device call that declares no duration and whose interface carries no timing."""


SIM_SPEEDUP_RANGE = (0.1, 1000.0)
"""The slowest and fastest a simulated run may go, as a multiple of real time."""


FRAMEWORK_SIM_SPEEDUP = 100.0
"""A local Orca install's deployment default. Its sim output is logs, so it runs fast."""

NO_WAITING = "off"
"""The deployment-default setting for a simulated run that does not wait at all."""


def parse_default_sim_speedup(raw: str | None, default: float) -> float | None:
    """A deployment-default setting: blank for ``default``, ``off`` for no waiting, else a speed-up."""
    text = (raw or "").strip()
    if text == "":
        return default
    if text.lower() == NO_WAITING:
        return None
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"sim speed-up must be a number or {NO_WAITING!r}; got {text!r}")
    return validate_sim_speedup(value)


def validate_sim_speedup(speedup: float) -> float:
    low, high = SIM_SPEEDUP_RANGE
    if (
        isinstance(speedup, bool)
        or not isinstance(speedup, (int, float))
        or not math.isfinite(speedup)
        or not low <= speedup <= high
    ):
        raise ValueError(
            f"sim_speedup must be a number from {low:g} to {high:g} "
            f"(1 is real time, 100 is a hundred times faster); got {speedup!r}"
        )
    return float(speedup)


def validate_optional_sim_speedup(speedup: float | None) -> float | None:
    """None, a run that does not wait at all, or a speed-up in SIM_SPEEDUP_RANGE."""
    return None if speedup is None else validate_sim_speedup(speedup)


class SimPacer:
    """One per simulated execution: the speed-up in force (None: no waiting)."""

    def __init__(self, speedup: float | None) -> None:
        self._speedup = validate_optional_sim_speedup(speedup)

    @property
    def speedup(self) -> float | None:
        return self._speedup

    def set_speedup(self, speedup: float) -> None:
        self._speedup = validate_sim_speedup(speedup)


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
    """What a thread seeds: the run's pacer for a sim run. Hardware paces LIVE, and a check run is never paced."""
    return pacer if run_mode is WorkflowRunMode.SIM else None


# Seeded at thread start next to `current_run_mode`: the run's pacer, or None for
# a LIVE thread. Unset (unit tests, direct dispatch) reads None: nothing paces by accident.
current_sim_pacer: ContextVar[SimPacer | None] = ContextVar(
    "current_sim_pacer", default=None,
)
