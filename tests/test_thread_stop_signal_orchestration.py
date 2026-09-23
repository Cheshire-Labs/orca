"""Catch-and-handle flow for ``_ThreadStopSignal`` inside
``ExecutingLabwareThread.start``.

The raise side (``_handle_thread_at_assigned_action_location`` raises
``_ThreadStopSignal`` when ``CoLabwareCoordinator.wait`` returns
``STOP_REQUESTED``) is covered by the coordinator unit tests and the
owner-aware-release tests. The catch side -- ``start()`` wrapping
``_run_method_loop`` in ``try / except _ThreadStopSignal:`` and
delegating to ``_handle_thread_stop`` so the thread lands at
``STOPPED`` -- is what this exercises.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orca.resource_models.tracked_lock import LockWait
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
    _ThreadStopSignal,
)
from orca.workflow_models.status_enums import LabwareThreadStatus


pytestmark = pytest.mark.asyncio


def _bare_thread_in_running_state() -> ExecutingLabwareThread:
    """Hand-construct a thread already past initialize_labware so
    ``start()`` proceeds straight to ``_run_method_loop``.
    """
    et = ExecutingLabwareThread.__new__(ExecutingLabwareThread)

    inner = MagicMock()
    inner.id = "t-stop-1"
    inner.name = "t-stop-1"
    inner.run_mode = MagicMock()
    et._thread = inner
    # __init__ is bypassed here; start() reads these to seed the
    # recoverable-timeout coordinator + execution-id ContextVars
    # (None = no runtime / not a workflow command).
    et._thread_incident_declarer = None
    et._context = None
    et._lock_wait = LockWait(owner=inner.name)

    et._stop_event = asyncio.Event()
    et._pause_request_event = asyncio.Event()
    et._held_at_start = False
    et._event_channel_registry = MagicMock()
    et._method_lane = MagicMock()
    et._method_lane.close = AsyncMock()
    et._holdover = MagicMock()
    et._labware_registry = None
    et._assigned_action = None
    et._move_action = None
    et._capacity_precheck_callback = None
    et._work_finished_hook = None
    et._completed_methods = []
    et._assigned_method = None
    et._thread_incident_declarer = None

    status_manager = MagicMock()
    status_manager.get_status.return_value = LabwareThreadStatus.AWAITING_CO_THREADS
    et._status_manager = status_manager

    state_machine = MagicMock()
    state_machine.current = LabwareThreadStatus.AWAITING_CO_THREADS
    state_machine.is_terminal.return_value = False
    et._state_machine = state_machine
    et._publish_status_to_status_manager = MagicMock()

    return et


async def test_thread_stop_signal_caught_lands_at_stopped() -> None:
    et = _bare_thread_in_running_state()
    handle_stop_called: list[bool] = []

    async def _raise_stop() -> None:
        raise _ThreadStopSignal()

    with patch.object(
        ExecutingLabwareThread, "_run_method_loop", new=AsyncMock(side_effect=_raise_stop),
    ), patch.object(
        ExecutingLabwareThread, "_release_all_held_reservations", new=MagicMock(),
    ), patch.object(
        ExecutingLabwareThread, "_handle_thread_stop",
        new=MagicMock(side_effect=lambda: handle_stop_called.append(True)),
    ), patch.object(
        ExecutingLabwareThread, "_yield_adapter",
        new=MagicMock(return_value=_empty_async_gen()),
    ):
        await et.start()

    assert handle_stop_called == [True], (
        "start() must call _handle_thread_stop when _run_method_loop raises _ThreadStopSignal"
    )


async def _empty_async_gen():
    if False:
        yield  # pragma: no cover
