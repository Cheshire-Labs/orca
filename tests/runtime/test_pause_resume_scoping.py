"""Bug CCC regression: ``pause_all_threads`` and ``resume_all_threads``
scope to the requested execution_id rather than the system-global
``executing_threads`` registry.

Pre-fix the methods iterated ``execution.system.executing_threads`` --
the system-wide thread registry shared across every execution -- so a
1-thread ``hello`` workflow's pause counters inflated to include peer
executions' threads: ``terminal_skipped:2`` on a workflow with one thread.

Post-fix both methods iterate ``_get_execution_threads(execution_id)``
which returns only threads that belong to ``execution_id``. The test
below stands up two stub executions with disjoint thread sets and
verifies the counters reflect only the requested execution's threads.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from orca.runtime.system_runtime import SystemRuntime
from orca.workflow_models.status_enums import LabwareThreadStatus


def _stub_thread(status: LabwareThreadStatus) -> MagicMock:
    t = MagicMock()
    t.status = status
    t.last_error = None
    return t


def _bind_runtime_with_per_execution_threads(
    threads_by_eid: dict[str, list[MagicMock]],
) -> Any:
    """Build a stand-in for ``SystemRuntime`` that ``pause_all_threads``
    can be called on.

    The methods under test only consult ``self._get_execution_threads``
    on this code path, so the stub overrides that single hook. We use
    ``MagicMock`` rather than constructing a real ``SystemRuntime``
    because the latter wires up an ``ISystem`` plus event bus that are
    irrelevant to the scoping assertion and add boot cost.
    """
    runtime = MagicMock(spec=SystemRuntime)
    runtime._get_execution_threads.side_effect = (
        lambda eid: list(threads_by_eid.get(eid, []))
    )
    # resume_all_threads consults the executions map to clear a stall episode,
    # and both methods read the execution to hold / release new threads.
    runtime._executions = {}
    runtime._get_execution.side_effect = lambda eid: SimpleNamespace(
        is_paused=False, new_threads_held=False, executing_workflow=None,
    )
    return runtime


def test_pause_all_threads_does_not_count_other_executions() -> None:
    """A pause request scoped to ``exec_a`` must not increment the
    ``terminal_skipped`` counter for ``exec_b``'s completed threads,
    even though the pre-fix iteration over the global registry would
    have. The reported symptom was ``terminal_skipped:2`` on a 1-thread
    workflow.
    """
    exec_a_running = _stub_thread(LabwareThreadStatus.EXECUTING_ACTION)
    exec_b_completed_1 = _stub_thread(LabwareThreadStatus.COMPLETED)
    exec_b_completed_2 = _stub_thread(LabwareThreadStatus.COMPLETED)

    runtime = _bind_runtime_with_per_execution_threads({
        "exec_a": [exec_a_running],
        "exec_b": [exec_b_completed_1, exec_b_completed_2],
    })

    result = SystemRuntime.pause_all_threads(runtime, "exec_a")

    assert result == {
        "pausing": 1,
        "already_paused": 0,
        "terminal_skipped": 0,
    }
    # exec_a's running thread was asked to pause; exec_b's threads
    # were not even consulted (would have incremented terminal_skipped
    # under the pre-fix system-global iteration).
    exec_a_running.request_pause.assert_called_once()
    exec_b_completed_1.request_pause.assert_not_called()
    exec_b_completed_2.request_pause.assert_not_called()


def test_pause_all_threads_counts_terminal_within_target_execution() -> None:
    """Within a single execution, terminated threads still count toward
    ``terminal_skipped`` (the documented ``COMPLETED`` / ``STOPPED``
    classification). The scope-tightening fix did not change that
    classification, only which threads are inspected.
    """
    running = _stub_thread(LabwareThreadStatus.EXECUTING_ACTION)
    completed = _stub_thread(LabwareThreadStatus.COMPLETED)
    stopped = _stub_thread(LabwareThreadStatus.STOPPED)
    paused = _stub_thread(LabwareThreadStatus.PAUSED)

    runtime = _bind_runtime_with_per_execution_threads({
        "exec_a": [running, completed, stopped, paused],
    })

    result = SystemRuntime.pause_all_threads(runtime, "exec_a")
    assert result == {
        "pausing": 1,
        "already_paused": 1,
        "terminal_skipped": 2,
    }


def test_pause_all_threads_forwards_reason_to_each_thread() -> None:
    """Defect 2: the runtime pausing itself (a stall, a declared deadlock, a
    recoverable timeout) must be distinguishable from an operator pause on
    read-back. Default stays "manual"; a caller pausing on the system's own
    behalf passes "system" through to every thread it asks to pause."""
    running = _stub_thread(LabwareThreadStatus.EXECUTING_ACTION)
    runtime = _bind_runtime_with_per_execution_threads({"exec_a": [running]})

    SystemRuntime.pause_all_threads(runtime, "exec_a", reason="system")

    running.request_pause.assert_called_once_with(reason="system", message=None)


def test_pause_all_threads_defaults_reason_to_manual() -> None:
    running = _stub_thread(LabwareThreadStatus.EXECUTING_ACTION)
    runtime = _bind_runtime_with_per_execution_threads({"exec_a": [running]})

    SystemRuntime.pause_all_threads(runtime, "exec_a")

    running.request_pause.assert_called_once_with(reason="manual", message=None)


def test_pause_execution_forwards_reason_to_pause_all_threads() -> None:
    """``pause_execution`` calls ``self.pause_all_threads(...)``, and
    ``runtime`` is a spec'd mock -- bind the real ``pause_all_threads`` onto
    it so that inner call exercises the actual forwarding, not a mock
    that swallows the reason silently."""
    running = _stub_thread(LabwareThreadStatus.EXECUTING_ACTION)
    runtime = _bind_runtime_with_per_execution_threads({"exec_a": [running]})
    runtime._get_execution.side_effect = lambda eid: SimpleNamespace(
        is_paused=False, new_threads_held=False, executing_workflow=None,
        paused_at=None,
    )
    runtime.pause_all_threads = SystemRuntime.pause_all_threads.__get__(runtime)

    SystemRuntime.pause_execution(runtime, "exec_a", reason="system")

    running.request_pause.assert_called_once_with(reason="system", message=None)


def test_resume_all_threads_does_not_count_other_executions() -> None:
    """Symmetric scoping check on ``resume_all_threads`` -- a resume
    request for ``exec_a`` must not classify ``exec_b``'s threads."""
    exec_a_paused = _stub_thread(LabwareThreadStatus.PAUSED)
    exec_b_completed = _stub_thread(LabwareThreadStatus.COMPLETED)
    # Default-stub `cancel_pending_pause` so the mock's auto-attribute
    # truthiness doesn't accidentally count toward `pause_cancelled` in
    # the non-paused/non-terminal branch this test does NOT exercise.
    exec_a_paused.cancel_pending_pause.return_value = False
    exec_b_completed.cancel_pending_pause.return_value = False

    runtime = _bind_runtime_with_per_execution_threads({
        "exec_a": [exec_a_paused],
        "exec_b": [exec_b_completed],
    })

    result = SystemRuntime.resume_all_threads(runtime, "exec_a")

    assert result == {
        "resumed": 1,
        "pause_cancelled": 0,
        "error_skipped": 0,
        "completed_skipped": 0,
    }
    exec_a_paused.resume_from_manual_pause.assert_called_once()
    exec_b_completed.resume_from_manual_pause.assert_not_called()


def test_resume_all_threads_cancels_pending_pause_on_non_paused_threads() -> None:
    """The pause+resume footgun: pause queues on every thread, and
    threads that are still MOVING / EXECUTING_ACTION /
    AWAITING_CO_THREADS at resume time keep the queued pause and latch
    into PAUSED *after* resume returned. Resume now also clears those
    queued pauses so the operator's pause+resume sequence is a no-op
    on threads that hadn't yet reached a safe point.
    """
    paused = _stub_thread(LabwareThreadStatus.PAUSED)
    paused.cancel_pending_pause.return_value = False
    moving = _stub_thread(LabwareThreadStatus.MOVING)
    moving.cancel_pending_pause.return_value = True
    awaiting = _stub_thread(LabwareThreadStatus.AWAITING_CO_THREADS)
    awaiting.cancel_pending_pause.return_value = True
    executing = _stub_thread(LabwareThreadStatus.EXECUTING_ACTION)
    # No queued pause on this one (e.g. it never had request_pause called).
    executing.cancel_pending_pause.return_value = False

    runtime = _bind_runtime_with_per_execution_threads({
        "exec_a": [paused, moving, awaiting, executing],
    })

    result = SystemRuntime.resume_all_threads(runtime, "exec_a")

    assert result == {
        "resumed": 1,
        "pause_cancelled": 2,
        "error_skipped": 0,
        "completed_skipped": 0,
    }
    paused.resume_from_manual_pause.assert_called_once()
    # Non-paused, non-terminal threads have their pending-pause query
    # called regardless of whether they actually had one queued.
    moving.cancel_pending_pause.assert_called_once()
    awaiting.cancel_pending_pause.assert_called_once()
    executing.cancel_pending_pause.assert_called_once()
