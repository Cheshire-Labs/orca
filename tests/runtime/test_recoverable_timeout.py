"""RecoverableTimeoutCoordinator: the engine's timeout race + operator decisions.

A device call that exceeds ``max_seconds`` parks awaiting an operator decision;
extend re-arms, abort fails, mark_complete synthesizes success. A call that
finishes within the bound is untouched. Paused siblings resume on every
post-timeout resolution (extend->complete, abort, mark_complete, late error).
"""

import asyncio

import pytest

from orca.resource_models.device_error import CommandTimeoutAbortedError
from orca.runtime.db import create_memory_engine
from orca.runtime.incident_service import IncidentService
from orca.runtime.incident_store import (
    IncidentCategory,
    IncidentSeverity,
    RecoverableTimeoutContext,
    RecoveryAction,
    SystemIncident,
)
from orca.runtime.sqlite_incident_store import SqliteIncidentStore
from orca.runtime.recoverable_timeout import (
    RecoverableTimeoutCoordinator,
    RecoverableTimeoutNotHeldError,
)

from tests.test_helpers import wait_until


class _FakeHost:
    """Real IncidentService-backed host so the fake honors the coordinator contract."""

    def __init__(self) -> None:
        self.declared: list[tuple[str, RecoverableTimeoutContext, str]] = []
        self.resumed: list[str] = []
        self.acked: list[str] = []
        self.faults_cleared: list[str] = []
        self._store = IncidentService(SqliteIncidentStore(create_memory_engine()))

    def declare_recoverable_timeout(
        self, execution_id: str, context: RecoverableTimeoutContext,
    ) -> SystemIncident:
        incident = self._store.record(
            category=IncidentCategory.RECOVERABLE_TIMEOUT,
            severity=IncidentSeverity.WARNING,
            message="recoverable timeout",
            detail=context,
            recovery_action=RecoveryAction.NONE,
            execution_id=execution_id,
        )
        self.declared.append((execution_id, context, incident.id))
        return incident

    def resume_all_threads(self, execution_id: str) -> dict[str, int]:
        self.resumed.append(execution_id)
        return {}

    async def clear_device_fault(self, device_id: str) -> None:
        self.faults_cleared.append(device_id)

    def acknowledge_incident(self, incident_id: str) -> bool:
        # Fire-and-forget enqueue; the durable acknowledge no longer raises.
        self._store.acknowledge(incident_id)
        self.acked.append(incident_id)
        return True


async def _wait_for_incident(host: _FakeHost, n: int = 1) -> str:
    for _ in range(400):
        if len(host.declared) >= n:
            return host.declared[n - 1][2]
        await asyncio.sleep(0.005)
    raise AssertionError("incident was not declared")


async def test_fast_call_returns_without_incident() -> None:
    host = _FakeHost()
    coord = RecoverableTimeoutCoordinator(host)

    async def quick() -> str:
        return "ok"

    result = await coord.run_with_timeout(
        "e1", "d1", "cmd", max_seconds=5.0, coro_factory=quick,
    )
    assert result == "ok"
    assert host.declared == []
    assert host.resumed == []


async def test_timeout_then_extend_completes_and_resumes() -> None:
    host = _FakeHost()
    coord = RecoverableTimeoutCoordinator(host)

    async def slow() -> str:
        # Stay in-flight until the timeout declares its incident, then finish
        # so extend resolves a still-parked call (no wall-clock guess).
        await wait_until(lambda: len(host.declared) >= 1, timeout=5.0)
        return "done"

    task = asyncio.ensure_future(
        coord.run_with_timeout(
            "e1", "d1", "cmd", max_seconds=0.05, coro_factory=slow,
        )
    )
    incident_id = await _wait_for_incident(host)
    coord.extend(incident_id, additional_seconds=5.0)
    assert await task == "done"
    assert host.acked == [incident_id]
    assert host.resumed == ["e1"]  # siblings resume once the call completes


async def test_extend_rejects_non_positive_additional_seconds() -> None:
    host = _FakeHost()
    coord = RecoverableTimeoutCoordinator(host)

    async def stuck() -> str:
        await asyncio.sleep(100)
        return "never"

    task = asyncio.ensure_future(
        coord.run_with_timeout(
            "e1", "d1", "cmd", max_seconds=0.05, coro_factory=stuck,
        )
    )
    incident_id = await _wait_for_incident(host)
    with pytest.raises(ValueError):
        coord.extend(incident_id, additional_seconds=0)
    with pytest.raises(ValueError):
        coord.extend(incident_id, additional_seconds=-1.0)
    coord.abort(incident_id, operator="t", reason="cleanup")
    with pytest.raises(CommandTimeoutAbortedError):
        await task


async def test_timeout_then_abort_raises_and_resumes() -> None:
    host = _FakeHost()
    coord = RecoverableTimeoutCoordinator(host)

    async def stuck() -> str:
        await asyncio.sleep(100)
        return "never"

    task = asyncio.ensure_future(
        coord.run_with_timeout(
            "e1", "d1", "cmd", max_seconds=0.05, coro_factory=stuck,
        )
    )
    incident_id = await _wait_for_incident(host)
    coord.abort(incident_id, operator="alice", reason="hung")
    with pytest.raises(CommandTimeoutAbortedError):
        await task
    assert host.resumed == ["e1"]
    assert host.acked == [incident_id]


async def test_timeout_then_mark_complete_returns_none_and_resumes() -> None:
    host = _FakeHost()
    coord = RecoverableTimeoutCoordinator(host)

    async def stuck() -> str:
        await asyncio.sleep(100)
        return "never"

    task = asyncio.ensure_future(
        coord.run_with_timeout(
            "e1", "d1", "cmd", max_seconds=0.05, coro_factory=stuck,
        )
    )
    incident_id = await _wait_for_incident(host)
    coord.mark_complete(incident_id, operator="bob", reason="did it by hand")
    assert await task is None
    assert host.resumed == ["e1"]


async def test_mark_complete_clears_the_fault_its_own_cancel_latched() -> None:
    """The operator looked at the device and said it finished.

    Draining the held dispatch cancels it, and the controller faults a device
    on a cancel because it cannot tell what the motion did. Leaving that fault
    standing would refuse the next workflow command on a device the operator
    just checked, with nothing saying a second call is owed.
    """
    host = _FakeHost()
    coord = RecoverableTimeoutCoordinator(host)

    async def stuck() -> str:
        await asyncio.sleep(100)
        return "never"

    task = asyncio.ensure_future(
        coord.run_with_timeout(
            "e1", "shaker_1", "shake", max_seconds=0.05, coro_factory=stuck,
        )
    )
    incident_id = await _wait_for_incident(host)
    coord.mark_complete(incident_id, operator="bob", reason="looked at it")

    assert await task is None
    assert host.faults_cleared == ["shaker_1"]


async def test_abort_leaves_the_fault_standing() -> None:
    """An abort says stop, not that the device is fine. Nobody looked."""
    host = _FakeHost()
    coord = RecoverableTimeoutCoordinator(host)

    async def stuck() -> str:
        await asyncio.sleep(100)
        return "never"

    task = asyncio.ensure_future(
        coord.run_with_timeout(
            "e1", "shaker_1", "shake", max_seconds=0.05, coro_factory=stuck,
        )
    )
    incident_id = await _wait_for_incident(host)
    coord.abort(incident_id, operator="bob", reason="stop")

    with pytest.raises(CommandTimeoutAbortedError):
        await task
    assert host.faults_cleared == []


async def test_extend_then_abort_re_declares_and_resumes() -> None:
    host = _FakeHost()
    coord = RecoverableTimeoutCoordinator(host)

    async def stuck() -> str:
        await asyncio.sleep(100)
        return "never"

    task = asyncio.ensure_future(
        coord.run_with_timeout(
            "e1", "d1", "cmd", max_seconds=0.05, coro_factory=stuck,
        )
    )
    first = await _wait_for_incident(host, 1)
    coord.extend(first, additional_seconds=0.05)
    second = await _wait_for_incident(host, 2)
    coord.abort(second, operator="alice", reason="enough")
    with pytest.raises(CommandTimeoutAbortedError):
        await task
    assert host.resumed == ["e1"]  # resumed exactly once


async def test_decision_for_unknown_incident_raises() -> None:
    coord = RecoverableTimeoutCoordinator(_FakeHost())
    with pytest.raises(RecoverableTimeoutNotHeldError):
        coord.abort("nope", operator="x", reason="y")


async def test_mark_complete_drains_held_task() -> None:
    """On mark_complete the held dispatch is cancelled AND awaited, so its
    CancelledError propagates (the controller's cancel-emit fires) and the
    task is retrieved -- no orphan 'exception never retrieved' leak."""
    host = _FakeHost()
    coord = RecoverableTimeoutCoordinator(host)
    cancelled = asyncio.Event()

    async def stuck() -> str:
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "never"

    task = asyncio.ensure_future(
        coord.run_with_timeout(
            "e1", "d1", "cmd", max_seconds=0.05, coro_factory=stuck,
        )
    )
    incident_id = await _wait_for_incident(host)
    coord.mark_complete(incident_id, operator="bob", reason="manual")
    assert await task is None
    assert cancelled.is_set()
