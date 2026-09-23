"""ExecutingLabwareThread.request_pause records WHO asked, so a later
non-error PAUSED status can report it instead of a hardcoded "manual".

See tests/runtime/test_thread_snapshot_pause_reason.py for how the read
side (ThreadSnapshot.pause_reason) consumes this hint.
"""
from unittest.mock import MagicMock

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.runtime.run_modes import WorkflowRunMode
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)
from orca.workflow_models.labware_threads.labware_thread import (
    LabwareThreadInstance,
)


def _build_executing_thread() -> ExecutingLabwareThread:
    location = Location("pad", resource=PlatePad("pad"))
    labware = LabwareInstance("plate_96", "96_well")
    thread = LabwareThreadInstance(
        labware=labware,
        start_location=location,
        end_locations=[location],
        run_mode=WorkflowRunMode.PURE_SIM,
    )
    context = MagicMock()
    context.execution_id = "exec-pause-reason"
    context.workflow_name = "wf-pause-reason"
    loc_service = MagicMock()
    loc_service.get_history.return_value = MagicMock()

    et = ExecutingLabwareThread(
        thread=thread,
        event_bus=MagicMock(),
        move_handler=MagicMock(),
        status_manager=MagicMock(),
        actions_resolver=MagicMock(),
        context=context,
        labware_location_service=loc_service,
    )
    et.publish_initial_status()
    return et


def test_default_pause_reason_is_manual() -> None:
    assert _build_executing_thread().pause_reason_hint == "manual"


def test_request_pause_records_the_given_reason() -> None:
    et = _build_executing_thread()

    et.request_pause(reason="system")

    assert et.pause_reason_hint == "system"


def test_a_later_request_pause_overwrites_the_earlier_reason() -> None:
    """A thread resumed from a system pause and later paused by an
    operator must report the NEW reason, not the stale one."""
    et = _build_executing_thread()
    et.request_pause(reason="system")

    et.request_pause()  # operator pause, default reason

    assert et.pause_reason_hint == "manual"


def test_request_pause_still_sets_the_pause_request_event() -> None:
    """``reason`` is additive: the cooperative-pause signal itself, read
    back here via the public ``cancel_pending_pause`` accessor, must still
    fire exactly as it did before this parameter existed."""
    et = _build_executing_thread()

    et.request_pause(reason="system")

    assert et.cancel_pending_pause() is True


def test_request_pause_records_the_given_message() -> None:
    et = _build_executing_thread()

    et.request_pause(reason="system", message="stalled: 3 threads blocked")

    assert et.pause_message == "stalled: 3 threads blocked"


def test_a_later_request_pause_with_no_message_clears_the_earlier_one() -> None:
    et = _build_executing_thread()
    et.request_pause(reason="system", message="stalled")

    et.request_pause()  # operator pause, no message

    assert et.pause_message is None


def test_hold_at_start_defaults_to_manual() -> None:
    et = _build_executing_thread()

    et.hold_at_start()

    assert et.pause_reason_hint == "manual"


def test_hold_at_start_records_the_given_reason() -> None:
    """A thread held before it starts (a pause that landed before it
    attached) must report the same reason a running thread would --
    previously it always read "manual" regardless of who held it."""
    et = _build_executing_thread()

    et.hold_at_start(reason="system")

    assert et.pause_reason_hint == "system"
