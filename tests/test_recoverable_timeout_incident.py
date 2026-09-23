"""Tests for the RECOVERABLE_TIMEOUT incident category and runtime hook.

A deployment declares one of these when an in-flight device command exceeds
its driver-advertised ``max_seconds`` without a response. The runtime
records the incident under :class:`IncidentCategory.RECOVERABLE_TIMEOUT`
and pauses every thread in the execution while the operator decides
whether to extend, abort, or mark complete via a hosted deployment's REST, MCP or
CLI surfaces.

These tests pin the orca-core half: the incident shape, the pause
fan-out, and the integration with the existing incident store. The
hosted-side operator surface (the three decision tools, the held-future
coordination, the boot policy) lives with the hosted deployment.
"""

import dataclasses
from unittest.mock import MagicMock

import pytest

from orca.daemon.schemas import IncidentDTO
from orca.runtime.incident_store import (
    IncidentCategory,
    IncidentSeverity,
    RecoverableTimeoutContext,
    RecoveryAction,
    SystemIncident,
)
from orca.runtime.system_runtime import SystemRuntime


class TestRecoverableTimeoutContext:
    def test_survives_incident_dto_serialization(self) -> None:
        """The context reaches operators only through ``IncidentDTO``
        (``GET /api/incidents`` and the MCP/CLI mirrors), which flattens
        ``SystemIncident.detail`` via ``dataclasses.asdict`` + JsonValue
        normalization. Pin that the five fields survive that real wire
        boundary, not just a self-construct."""
        ctx = RecoverableTimeoutContext(
            device_id="shaker_1",
            command="shake",
            command_id="cmd-abc-123",
            elapsed_seconds=7250.0,
            max_seconds=7200.0,
        )
        incident = SystemIncident(
            id="inc-1",
            timestamp=0.0,
            category=IncidentCategory.RECOVERABLE_TIMEOUT,
            severity=IncidentSeverity.WARNING,
            execution_id="exec-1",
            thread_id=None,
            message="shaker_1 shake exceeded max_seconds; operator decision required",
            detail=ctx,
            recovery_action=RecoveryAction.NONE,
            acknowledged=False,
        )

        dto = IncidentDTO.from_incident(incident)

        assert dto.detail == {
            "device_id": "shaker_1",
            "command": "shake",
            "command_id": "cmd-abc-123",
            "elapsed_seconds": 7250.0,
            "max_seconds": 7200.0,
        }

    def test_frozen(self) -> None:
        ctx = RecoverableTimeoutContext(
            device_id="shaker_1",
            command="shake",
            command_id="cmd-1",
            elapsed_seconds=10.0,
            max_seconds=5.0,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.device_id = "other"  # type: ignore[misc]


class TestIncidentCategoryRecoverableTimeoutEnumMember:
    def test_value_matches_name(self) -> None:
        assert IncidentCategory.RECOVERABLE_TIMEOUT.value == "RECOVERABLE_TIMEOUT"

    def test_distinct_from_other_categories(self) -> None:
        # Make sure the new category doesn't collide with the existing
        # UNRESOLVABLE_DEADLOCK category that also fires pause_all_threads.
        assert (
            IncidentCategory.RECOVERABLE_TIMEOUT
            is not IncidentCategory.UNRESOLVABLE_DEADLOCK
        )


class TestDeclareRecoverableTimeoutRecordsIncident:
    """SystemRuntime.declare_recoverable_timeout records an incident with
    the right category, detail, and execution-id binding, and fans out
    pause_all_threads(execution_id).

    Uses MagicMock for the System so we can verify the fan-out without
    standing up a full runtime fixture. The real production wiring is
    covered by the hosted-side integration test.
    """

    def _make_runtime(self) -> tuple[SystemRuntime, MagicMock]:
        """Build a runtime with pause_all_threads stubbed so the
        fan-out doesn't require a registered execution. The pause
        behavior itself is covered by ``TestDeclareRecoverableTimeoutPausesExecution``
        below + the existing pause_all_threads tests."""
        system = MagicMock()
        runtime = SystemRuntime(system)
        # Replace the bound method with a MagicMock for fan-out assertions.
        runtime.pause_all_threads = MagicMock(  # type: ignore[method-assign]
            return_value={
                "pausing": 0, "already_paused": 0, "terminal_skipped": 0,
            },
        )
        return runtime, system

    def test_records_incident_with_recoverable_timeout_category(self) -> None:
        runtime, _system = self._make_runtime()
        ctx = RecoverableTimeoutContext(
            device_id="shaker_1",
            command="shake",
            command_id="cmd-abc-123",
            elapsed_seconds=7300.0,
            max_seconds=7200.0,
        )

        incident: SystemIncident = runtime.declare_recoverable_timeout(
            execution_id="exec-1", context=ctx,
        )

        assert incident.category is IncidentCategory.RECOVERABLE_TIMEOUT
        assert incident.severity is IncidentSeverity.WARNING
        assert incident.detail == ctx
        assert incident.execution_id == "exec-1"
        assert incident.recovery_action is RecoveryAction.NONE

    def test_records_human_readable_message(self) -> None:
        runtime, _system = self._make_runtime()
        ctx = RecoverableTimeoutContext(
            device_id="centrifuge_1",
            command="centrifuge",
            command_id="cmd-xyz",
            elapsed_seconds=11000.0,
            max_seconds=10800.0,
        )

        incident = runtime.declare_recoverable_timeout(
            execution_id="exec-1", context=ctx,
        )

        assert "centrifuge_1" in incident.message
        assert "centrifuge" in incident.message
        assert "operator" in incident.message.lower()

    async def test_incident_is_retrievable_from_store(self) -> None:
        runtime, _system = self._make_runtime()
        ctx = RecoverableTimeoutContext(
            device_id="shaker_1", command="shake", command_id="cmd-1",
            elapsed_seconds=10.0, max_seconds=5.0,
        )

        recorded = runtime.declare_recoverable_timeout(
            execution_id="exec-1", context=ctx,
        )

        # Use the public incidents facade, not the private _incident_store
        # attribute, so the test stays decoupled from internal storage.
        fetched = await runtime.incidents.get(recorded.id)
        assert fetched.id == recorded.id
        assert fetched.detail == ctx

    def test_multiple_invocations_record_distinct_incidents(self) -> None:
        runtime, _system = self._make_runtime()
        ctx = RecoverableTimeoutContext(
            device_id="shaker_1", command="shake", command_id="cmd-1",
            elapsed_seconds=10.0, max_seconds=5.0,
        )

        a = runtime.declare_recoverable_timeout(
            execution_id="exec-1", context=ctx,
        )
        b = runtime.declare_recoverable_timeout(
            execution_id="exec-1", context=ctx,
        )

        assert a.id != b.id


class TestDeclareRecoverableTimeoutPausesExecution:
    """The fan-out invariant: declare_recoverable_timeout calls
    pause_all_threads(execution_id) so siblings of the blocked thread
    don't race ahead while the operator deliberates."""

    def test_pauses_threads_for_given_execution(self) -> None:
        system = MagicMock()
        runtime = SystemRuntime(system)

        # Stub pause_all_threads to record the call without standing up
        # a registered execution; the real method is covered separately.
        pause_calls: list[tuple[str, str, str | None]] = []

        def _stub(execution_id: str, reason: str = "manual", message: str | None = None) -> dict:
            pause_calls.append((execution_id, reason, message))
            return {"pausing": 0, "already_paused": 0, "terminal_skipped": 0}

        runtime.pause_all_threads = _stub  # type: ignore[method-assign]

        ctx = RecoverableTimeoutContext(
            device_id="shaker_1", command="shake", command_id="cmd-1",
            elapsed_seconds=10.0, max_seconds=5.0,
        )

        incident = runtime.declare_recoverable_timeout(
            execution_id="exec-A", context=ctx,
        )

        assert pause_calls == [("exec-A", "system", incident.message)], (
            "a recoverable timeout is not an operator pausing the run; the "
            "sibling threads' state must say so, not read as manual, and "
            "must carry the timeout's own cause"
        )
