"""A stopped execution says so on every read surface.

`stop_execution` sets the execution-level pause latch immediately. The latch is
orthogonal to the phase, so the execution stays ACCEPTING or DRAINING and every
read surface reported a stopped run as still running. An operator watching the
panel saw nothing change and read that as "stop did nothing".

The status snapshot, the detail snapshot and the list wire model now carry the
latch, who set it, and whether a second confirmed stop would abort.

Harness (a hanging shake that keeps the execution live) is shared with
test_two_phase_stop.py, which pins the stop semantics themselves.
"""

from orca.operations.execution import ListExecutionsOperation
from orca.operations.execution_models import (
    ExecutionRecordModel,
    GetExecutionDetailResponse,
    ListExecutionsRequest,
)
from orca.runtime.execution import ExecutionPhase
from tests.test_two_phase_stop import (
    _build_hanging_owner_system,
    _submit_and_run_to_hang,
)


async def test_a_running_execution_reads_unpaused() -> None:
    runtime, workflow, device, _bus = await _build_hanging_owner_system()
    eid = await _submit_and_run_to_hang(runtime, workflow, device)
    try:
        status = runtime.get_execution_status(eid)

        assert status.paused is False
        assert status.pause_reason is None
        assert status.abort_armed is False
    finally:
        device.release.set()
        await runtime.shutdown()


async def test_the_first_stop_shows_up_on_the_status_snapshot() -> None:
    runtime, workflow, device, _bus = await _build_hanging_owner_system()
    eid = await _submit_and_run_to_hang(runtime, workflow, device)
    try:
        await runtime.stop_execution(eid)

        status = runtime.get_execution_status(eid)
        assert status.status is ExecutionPhase.ACCEPTING, (
            "the phase is unchanged by design; that is why the latch has to "
            "ride alongside it"
        )
        assert status.paused is True
        assert status.pause_reason == "manual"
        assert status.abort_armed is True

        wire = ExecutionRecordModel.from_status(status)
        assert wire.paused is True
        assert wire.pause_reason == "manual"
        assert wire.abort_armed is True
    finally:
        device.release.set()
        await runtime.shutdown()


async def test_the_detail_snapshot_carries_the_latch() -> None:
    runtime, workflow, device, _bus = await _build_hanging_owner_system()
    eid = await _submit_and_run_to_hang(runtime, workflow, device)
    try:
        await runtime.stop_execution(eid)

        detail = runtime.get_execution_detail(eid)
        assert detail.paused is True
        assert detail.abort_armed is True

        wire = GetExecutionDetailResponse.from_detail(detail)
        assert wire.paused is True
        assert wire.pause_reason == "manual"
        assert wire.abort_armed is True
    finally:
        device.release.set()
        await runtime.shutdown()


async def test_the_execution_list_carries_the_latch() -> None:
    runtime, workflow, device, _bus = await _build_hanging_owner_system()
    eid = await _submit_and_run_to_hang(runtime, workflow, device)
    try:
        await runtime.stop_execution(eid)

        listed = await ListExecutionsOperation(runtime).run(ListExecutionsRequest())
        row = next(r for r in listed.executions if r.id == eid)

        assert row.paused is True
        assert row.abort_armed is True
    finally:
        device.release.set()
        await runtime.shutdown()


async def test_resume_clears_the_latch_and_disarms_on_the_wire() -> None:
    runtime, workflow, device, _bus = await _build_hanging_owner_system()
    eid = await _submit_and_run_to_hang(runtime, workflow, device)
    try:
        await runtime.stop_execution(eid)
        runtime.resume_execution(eid)

        status = runtime.get_execution_status(eid)
        assert status.paused is False
        assert status.pause_reason is None
        assert status.abort_armed is False
    finally:
        device.release.set()
        await runtime.shutdown()


async def test_a_system_pause_says_the_system_set_it() -> None:
    """A stall or an unresolvable deadlock pauses the execution too. The
    operator needs to know nobody clicked stop."""
    runtime, workflow, device, _bus = await _build_hanging_owner_system()
    eid = await _submit_and_run_to_hang(runtime, workflow, device)
    try:
        runtime.pause_execution(eid, reason="system", message="stall detected")

        status = runtime.get_execution_status(eid)
        assert status.paused is True
        assert status.pause_reason == "system"
        assert status.abort_armed is False
    finally:
        device.release.set()
        await runtime.shutdown()
