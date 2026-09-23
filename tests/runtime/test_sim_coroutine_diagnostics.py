"""Tests for the sim-only un-awaited-coroutine diagnostics.

A missing `await` on an async accessor (e.g. `ctx.labware.can_continue()`)
is silent: the call returns a truthy coroutine and the only feedback is a
late `RuntimeWarning: coroutine '...' was never awaited` at GC time, far
from the call site. These tests pin that the sim diagnostics make that
footgun loud: the warning is routed through logging and the running loop
is put into asyncio debug mode so the coroutine's creation traceback is
captured.
"""

import asyncio
import gc
import logging
import warnings
from collections.abc import Iterator

import pytest

from orca.runtime import sim_diagnostics
from orca.runtime.sim_diagnostics import (
    enable_sim_coroutine_diagnostics,
    maybe_enable_sim_coroutine_diagnostics_from_env,
    sim_coroutine_diagnostics_enabled,
)


@pytest.fixture(autouse=True)
def _restore_global_diagnostics_state() -> Iterator[None]:
    """Diagnostics state is process-global; isolate each test fully.

    ``enable_sim_coroutine_diagnostics`` flips a module flag,
    ``logging.captureWarnings``, and the warning filter stack -- all
    process-global. Without a restore, one test that enables diagnostics
    would silently pre-arm every later test (and the rest of the suite).
    ``catch_warnings`` restores the filter list and ``showwarning``; the
    module flag is snapshotted by hand. (Loop debug mode does not leak: each
    async test runs on its own function-scoped event loop.)
    """
    was_enabled = sim_diagnostics._enabled
    sim_diagnostics._enabled = False
    with warnings.catch_warnings():
        try:
            yield
        finally:
            sim_diagnostics._enabled = was_enabled
            logging.captureWarnings(False)


class _RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


async def _dummy_accessor() -> bool:
    return True


@pytest.mark.asyncio
async def test_enables_asyncio_debug_on_running_loop() -> None:
    loop = asyncio.get_running_loop()
    was_debug = loop.get_debug()
    try:
        enable_sim_coroutine_diagnostics()
        assert loop.get_debug() is True
        assert sim_coroutine_diagnostics_enabled() is True
    finally:
        loop.set_debug(was_debug)


@pytest.mark.asyncio
async def test_unawaited_coroutine_surfaces_loud_warning() -> None:
    """An un-awaited coroutine is routed to the py.warnings logger by the feature.

    No test-local ``simplefilter``: the feature itself installs the "always"
    filter and ``captureWarnings(True)``, so the GC-time RuntimeWarning lands
    on ``py.warnings``. Saving/restoring ``showwarning`` works around pytest's
    warnings plugin owning the hook during a test, the same way a real sim run
    (pytest absent) routes it.
    """
    loop = asyncio.get_running_loop()
    was_debug = loop.get_debug()
    handler = _RecordingHandler()
    py_warnings = logging.getLogger("py.warnings")
    py_warnings.addHandler(handler)
    saved_showwarning = warnings.showwarning
    enable_sim_coroutine_diagnostics()
    try:
        # Forget the await: a list holds the coroutine, then drop it so GC
        # destroys it un-awaited (the footgun shape) without an unused local.
        pending = [_dummy_accessor()]
        pending.clear()
        gc.collect()
    finally:
        warnings.showwarning = saved_showwarning
        py_warnings.removeHandler(handler)
        loop.set_debug(was_debug)
    messages = [rec.getMessage() for rec in handler.records]
    assert any("never awaited" in m for m in messages), messages


@pytest.mark.asyncio
async def test_routes_runtime_warnings_through_logging() -> None:
    """captureWarnings(True) routes RuntimeWarnings to the py.warnings logger.

    pytest's warnings plugin owns ``warnings.showwarning`` during a test, so
    we drive the route the way a real sim run does (pytest absent): snapshot
    the hook, install the ``captureWarnings(True)`` route, emit, and confirm
    the warning text lands on the ``py.warnings`` logger.
    """
    enable_sim_coroutine_diagnostics()
    handler = _RecordingHandler()
    py_warnings = logging.getLogger("py.warnings")
    py_warnings.addHandler(handler)
    saved_showwarning = warnings.showwarning
    try:
        logging.captureWarnings(False)
        logging.captureWarnings(True)
        warnings.warn(
            "coroutine 'x' was never awaited", RuntimeWarning, stacklevel=1,
        )
    finally:
        warnings.showwarning = saved_showwarning
        py_warnings.removeHandler(handler)
    assert any(
        "never awaited" in rec.getMessage() for rec in handler.records
    ), [rec.getMessage() for rec in handler.records]


@pytest.mark.asyncio
async def test_idempotent_second_call_is_a_no_op() -> None:
    """The `if _enabled: return` guard makes a second enable do no work.

    Prove the guard bites: after the first enable arms loop debug, force
    debug back off, then call enable again. The guard short-circuits before
    touching the loop, so debug stays off. Without the guard the second call
    would re-run `loop.set_debug(True)` and the assertion would fail.
    """
    loop = asyncio.get_running_loop()
    was_debug = loop.get_debug()
    try:
        enable_sim_coroutine_diagnostics()
        assert loop.get_debug() is True

        loop.set_debug(False)
        enable_sim_coroutine_diagnostics()

        assert loop.get_debug() is False
        assert sim_coroutine_diagnostics_enabled() is True
    finally:
        loop.set_debug(was_debug)


@pytest.mark.asyncio
async def test_env_off_deployment_never_enables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sim-diagnostic-off deployment must not enable asyncio debug.

    The decided gate is deployment-level (the env flag), never the
    submission's run_mode. With the flag unset, no execution entrypoint --
    LIVE or sim -- may flip the process-global loop debug + warning routing.
    This is the regression that protects a LIVE run sharing the loop with
    an earlier sim submission.
    """
    monkeypatch.delenv("ORCA_SIM_COROUTINE_DIAGNOSTICS", raising=False)
    loop = asyncio.get_running_loop()
    loop.set_debug(False)
    maybe_enable_sim_coroutine_diagnostics_from_env()
    assert sim_coroutine_diagnostics_enabled() is False
    assert loop.get_debug() is False


@pytest.mark.asyncio
async def test_env_on_deployment_enables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting the deployment-level env flag opts the sim deployment in."""
    monkeypatch.setenv("ORCA_SIM_COROUTINE_DIAGNOSTICS", "1")
    loop = asyncio.get_running_loop()
    maybe_enable_sim_coroutine_diagnostics_from_env()
    assert sim_coroutine_diagnostics_enabled() is True
    assert loop.get_debug() is True
