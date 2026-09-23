"""ThreadSnapshot.pause_reason distinguishes a person pausing a run from the
runtime pausing itself.

Defect 2 from the 2026-08-28 bench incident: a stall-detected pause called
the exact same `pause_execution` an operator uses, so every reader saw
`pause_reason: manual` with no error -- indistinguishable from a person
having clicked Pause. The same collapse existed at three more call sites
that were not named in the original incident (declared deadlock, a
recoverable device-command timeout pausing siblings, an orphaned-slot
protection pause) -- all reuse `ExecutingLabwareThread.request_pause`, which
previously took no argument for who is asking.
"""
from unittest.mock import MagicMock

from orca.runtime.status_builders import _build_thread_snapshot
from orca.workflow_models.status_enums import LabwareThreadStatus


def _make_paused_thread(
    *, last_error: Exception | None, pause_reason_hint: str,
) -> MagicMock:
    thread = MagicMock()
    thread.id = "t1"
    thread.name = "plate_1-abcd"
    thread.status = LabwareThreadStatus.PAUSED
    thread.current_location.name = "pad_1"
    thread.assigned_method = None
    thread.assigned_action = None
    thread.completed_methods = []
    thread.last_error = last_error
    thread.labware_template = None
    thread.pause_reason_hint = pause_reason_hint
    thread.paused_device_command = None
    thread.pause_message = None
    thread.pause_site = None
    return thread


def test_a_manual_pause_reports_manual() -> None:
    thread = _make_paused_thread(last_error=None, pause_reason_hint="manual")

    assert _build_thread_snapshot(thread).pause_reason == "manual"


def test_a_system_triggered_pause_reports_system_not_manual() -> None:
    """The regression: pre-fix this read "manual" no matter who asked."""
    thread = _make_paused_thread(last_error=None, pause_reason_hint="system")

    assert _build_thread_snapshot(thread).pause_reason == "system"


def test_an_error_pause_reports_error_regardless_of_the_hint() -> None:
    """An action failure always reports "error", even if the thread happens
    to carry a stale "system" hint from an earlier system-triggered pause
    it was later recovered from."""
    thread = _make_paused_thread(
        last_error=RuntimeError("driver said no"), pause_reason_hint="system",
    )

    assert _build_thread_snapshot(thread).pause_reason == "error"


def test_a_running_thread_has_no_pause_reason() -> None:
    thread = _make_paused_thread(last_error=None, pause_reason_hint="manual")
    thread.status = LabwareThreadStatus.EXECUTING_ACTION

    assert _build_thread_snapshot(thread).pause_reason is None
