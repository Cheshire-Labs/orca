"""Sim-only diagnostics that make the un-awaited-coroutine footgun loud.

A missing ``await`` on an async accessor (the canonical case is
``ctx.labware.can_continue()``) is silent: the bare call returns a truthy
coroutine, so ``if not ...: break`` becomes dead code and nothing raises at
the call site. The only feedback is a late ``RuntimeWarning: coroutine
'...' was never awaited`` emitted by the garbage collector, decoupled from
where the mistake was made.

``enable_sim_coroutine_diagnostics`` turns that whisper into a loud signal
for simulation runs, where catching authoring mistakes early is the whole
point:

* ``asyncio`` debug mode on the running loop makes the loop capture each
  coroutine's *creation* traceback and log it when the coroutine is
  destroyed un-awaited, so the warning points at the call site.
* ``logging.captureWarnings(True)`` plus an "always" filter routes the
  ``RuntimeWarning`` through the ``py.warnings`` logger instead of the
  default stderr-once behaviour, so it rides the same log stream operators
  already watch during a sim run.

It is a WARNING surface, not a hard failure: a stray un-awaited coroutine
should not abort a long sim run, and erroring would risk masking real
results.

The toggle is DEPLOYMENT-level, never per-submission. ``asyncio`` debug
mode and the warning filter are process-global and live on the shared
event loop, so gating on a single submission's ``run_mode`` would let one
PURE_SIM submission turn on slow-callback debug overhead for every later
LIVE submission on the same loop. The two deployment-level entry points
are: the daemon ``/load`` route (gated on ``body.sim``) and the
``ORCA_SIM_COROUTINE_DIAGNOSTICS`` env flag (default off) for SDK / standalone
callers that drive the executors without the daemon. A LIVE deployment
leaves both off, so production is untouched.
"""

import asyncio
import logging
import os
import warnings

logger = logging.getLogger("orca.sim_diagnostics")

_ENV_FLAG = "ORCA_SIM_COROUTINE_DIAGNOSTICS"
_ENV_TRUE = frozenset({"1", "true", "yes", "on"})

_enabled = False


def enable_sim_coroutine_diagnostics() -> None:
    """Make un-awaited coroutines loud for the current sim run. Idempotent.

    Must be called from within a running event loop (every orca execution
    entrypoint runs inside one). Safe to call repeatedly; only the first
    call does work.
    """
    global _enabled
    if _enabled:
        return

    warnings.simplefilter("always", RuntimeWarning)
    logging.captureWarnings(True)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.set_debug(True)

    _enabled = True
    logger.info(
        "sim coroutine diagnostics enabled: un-awaited coroutines will be "
        "surfaced loudly with their creation traceback",
    )


def sim_coroutine_diagnostics_requested_via_env() -> bool:
    """True when ``ORCA_SIM_COROUTINE_DIAGNOSTICS`` opts the deployment in."""
    return os.environ.get(_ENV_FLAG, "").strip().lower() in _ENV_TRUE


def maybe_enable_sim_coroutine_diagnostics_from_env() -> None:
    """Enable the diagnostics iff the deployment-level env flag is set.

    Called from each SDK execution entrypoint. The gate is deployment-wide
    (the env flag), never per-submission ``run_mode``: the flag flips
    process-global asyncio debug + warning routing on the shared loop, so a
    per-submission gate would let one sim run degrade later LIVE runs. A
    LIVE deployment leaves the env flag unset and stays untouched.
    """
    if sim_coroutine_diagnostics_requested_via_env():
        enable_sim_coroutine_diagnostics()


def sim_coroutine_diagnostics_enabled() -> bool:
    """True once :func:`enable_sim_coroutine_diagnostics` has run this process."""
    return _enabled
