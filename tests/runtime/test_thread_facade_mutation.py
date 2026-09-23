"""Unit tests for ThreadFacade mutation methods.

Tests verify the facade's wiring to ISystem, the @dangerous confirm gate,
and per-thread serialization of concurrent mutations. End-to-end mutation
semantics are covered by tests/test_mutation_coordinator.py.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.runtime.danger import ConfirmationRequired
from orca.runtime.facades.threads import ThreadFacade
from orca.workflow_models.action_context import ActionContext

from tests.mock import UniversalMockDevice
from tests.test_helpers import wait_until


def _make_facade(thread_ids: list[str]) -> tuple[ThreadFacade, MagicMock, MagicMock]:
    """Build a ThreadFacade with mocked runtime + system.

    `thread_ids` populates the runtime's `list_threads` so
    `_validate_thread_in_execution` accepts the calls.
    """
    runtime = MagicMock()
    runtime.list_threads.return_value = [
        MagicMock(id=tid) for tid in thread_ids
    ]
    system = MagicMock()
    system.abort_method = AsyncMock()
    facade = ThreadFacade(runtime, system)
    return facade, runtime, system


class TestAbortMethod:
    """ThreadFacade.abort_method delegates to ISystem.abort_method.

    Mirrors the skip_method facade method but for in-progress methods that
    cannot be skipped (already started). Required by the runtime mutation
    surface; missing from IThreadFacade until this work.
    """

    @pytest.mark.asyncio
    async def test_abort_method_by_name_calls_system(self) -> None:
        facade, _runtime, system = _make_facade(["t1"])

        await facade.abort_method(
            "exec-1", "t1", method_name="incubate",
            reason="bench requested abort", confirm=True,
        )

        system.abort_method.assert_awaited_once_with("t1", None, "incubate")

    @pytest.mark.asyncio
    async def test_abort_method_by_id_calls_system(self) -> None:
        facade, _runtime, system = _make_facade(["t1"])

        await facade.abort_method(
            "exec-1", "t1", method_id="m-42",
            reason="bench requested abort", confirm=True,
        )

        system.abort_method.assert_awaited_once_with("t1", "m-42", None)

    @pytest.mark.asyncio
    async def test_abort_method_requires_confirm(self) -> None:
        facade, _runtime, _system = _make_facade(["t1"])

        with pytest.raises(ConfirmationRequired):
            await facade.abort_method(
                "exec-1", "t1", method_name="incubate",
            )

    @pytest.mark.asyncio
    async def test_abort_method_rejects_thread_not_in_execution(self) -> None:
        facade, _runtime, system = _make_facade(["other-thread"])

        with pytest.raises(KeyError, match="not part of execution"):
            await facade.abort_method(
                "exec-1", "missing-thread", method_name="incubate",
                reason="bench requested abort", confirm=True,
            )
        system.abort_method.assert_not_awaited()


class TestLockEviction:
    """Per-thread mutation locks are evicted when the thread reaches a terminal
    state (COMPLETED / STOPPED / ABORTED). The eviction is wired by
    SystemRuntime subscribing the facade's `on_thread_terminal_event`
    listener to its system event bus; here we drive the listener directly
    with synthetic RuntimeEvents, since unit-level coverage doesn't need
    the full runtime stack.
    """

    def _make_event(self, *, status: str, entity_id: str = "t1") -> object:
        from orca.events.execution_context import WorkflowExecutionContext
        from orca.events.runtime_event import RuntimeEvent

        return RuntimeEvent(
            event_name=f"THREAD.{entity_id}.{status}",
            execution_id="exec-1",
            timestamp=0.0,
            entity_type="THREAD",
            entity_id=entity_id,
            status=status,
            context=WorkflowExecutionContext(
                execution_id="exec-1", workflow_name="wf",
            ),
        )

    @pytest.mark.asyncio
    async def test_completed_event_evicts_lock(self) -> None:
        facade, _runtime, _system = _make_facade(["t1"])
        # Take a lock so an entry exists.
        facade._lock_for("t1")
        assert "t1" in facade._mutation_locks

        facade.on_thread_terminal_event(self._make_event(status="COMPLETED"))

        assert "t1" not in facade._mutation_locks

    @pytest.mark.asyncio
    async def test_stopped_event_evicts_lock(self) -> None:
        facade, _runtime, _system = _make_facade(["t1"])
        facade._lock_for("t1")

        facade.on_thread_terminal_event(self._make_event(status="STOPPED"))

        assert "t1" not in facade._mutation_locks

    @pytest.mark.asyncio
    async def test_aborted_event_evicts_lock(self) -> None:
        """ABORTED is terminal too: a thread ended via ABORT_THREAD must drop
        its lock, or every operator-aborted thread leaks one for the process
        lifetime."""
        facade, _runtime, _system = _make_facade(["t1"])
        facade._lock_for("t1")

        facade.on_thread_terminal_event(self._make_event(status="ABORTED"))

        assert "t1" not in facade._mutation_locks

    @pytest.mark.asyncio
    async def test_recover_unknown_thread_does_not_allocate_lock(self) -> None:
        """recover() validates membership before _lock_for, so a mistyped id
        raises without leaving an un-evictable lock entry behind (no terminal
        event will ever fire for an id that never existed)."""
        from orca.workflow_models.status_enums import RecoveryDecision

        facade, runtime, _system = _make_facade(["t1"])

        with pytest.raises(KeyError, match="not part of execution"):
            await facade.recover(
                "exec-1", "missing-thread", RecoveryDecision.RETRY, confirm=True,
            )

        assert "missing-thread" not in facade._mutation_locks
        runtime.recover_thread.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_terminal_event_does_not_evict(self) -> None:
        """Mid-life events (PAUSED, EXECUTING_ACTION, ...) leave the lock
        in place so concurrent operators on a paused thread still serialize.
        """
        facade, _runtime, _system = _make_facade(["t1"])
        facade._lock_for("t1")

        facade.on_thread_terminal_event(self._make_event(status="PAUSED"))
        facade.on_thread_terminal_event(self._make_event(status="EXECUTING_ACTION"))

        assert "t1" in facade._mutation_locks

    @pytest.mark.asyncio
    async def test_non_thread_event_ignored(self) -> None:
        """METHOD / ACTION / WORKFLOW events do not affect thread locks."""
        from orca.events.execution_context import WorkflowExecutionContext
        from orca.events.runtime_event import RuntimeEvent

        facade, _runtime, _system = _make_facade(["t1"])
        facade._lock_for("t1")

        method_event = RuntimeEvent(
            event_name="METHOD.m1.COMPLETED",
            execution_id="exec-1", timestamp=0.0,
            entity_type="METHOD", entity_id="m1", status="COMPLETED",
            context=WorkflowExecutionContext(
                execution_id="exec-1", workflow_name="wf",
            ),
        )
        facade.on_thread_terminal_event(method_event)

        assert "t1" in facade._mutation_locks

    @pytest.mark.asyncio
    async def test_eviction_idempotent_on_repeat_terminal_event(self) -> None:
        """A second terminal event for an already-evicted thread is a clean
        no-op: the lock stays gone, no KeyError. Duplicate terminal events are
        real (e.g. STOPPED then ABORTED on a torn-down thread).
        """
        facade, _runtime, _system = _make_facade(["t1"])
        facade._lock_for("t1")
        assert "t1" in facade._mutation_locks

        facade.on_thread_terminal_event(self._make_event(status="COMPLETED"))
        assert "t1" not in facade._mutation_locks

        facade.on_thread_terminal_event(self._make_event(status="ABORTED"))
        assert "t1" not in facade._mutation_locks

    @pytest.mark.asyncio
    async def test_runtime_wires_listener_to_system_event_bus(self) -> None:
        """Integration: SystemRuntime subscribes the eviction listener to
        its system event bus during construction. Emitting a synthetic
        terminal event through the bus must clear the lock entry --
        proves the subscribe call is in place, not just the listener
        method.
        """
        from orca.runtime.system_runtime import SystemRuntime
        from tests.test_system_runtime import _build_simple_system

        system, _ = await _build_simple_system()
        rt = SystemRuntime(system)
        try:
            rt.threads._lock_for("t-stale")
            assert "t-stale" in rt.threads._mutation_locks

            rt._system_event_bus.emit(self._make_event(
                status="COMPLETED", entity_id="t-stale",
            ))

            assert "t-stale" not in rt.threads._mutation_locks
        finally:
            if rt.state.name == "RUNNING":
                await rt.shutdown(confirm=True)


class TestInsertMethodRoutesAsync:
    """L5: ThreadFacade.insert_method routes through insert_method_async
    (not the sync insert_method that uses ThreadPoolExecutor +
    future.result(timeout=10.0)). This is the wire-call path; blocking
    the daemon's event loop on every operator click is unacceptable.
    """

    @pytest.mark.asyncio
    async def test_facade_calls_system_insert_method_async(self) -> None:
        from collections.abc import AsyncGenerator

        from orca.workflow_models.action_template import ActionTemplate
        from orca.workflow_models.method_context import MethodContext
        from orca.workflow_models.method_template import MethodTemplate
        from orca.workflow_models.mutation_position import AtTail

        facade, _runtime, system = _make_facade(["t1"])
        system.insert_method_async = AsyncMock()

        # Real, not MagicMock(spec=...): a mock's auto-generated to_dict()
        # recurses forever through @dangerous's JSON-safe audit capture.
        async def _func(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            return
            yield  # pragma: no cover -- never actually iterated

        template: MethodTemplate = MethodTemplate("mock_method", _func)

        await facade.insert_method(
            "exec-1", "t1", template, AtTail(),
            reason="bench requested insert", confirm=True,
        )

        system.insert_method_async.assert_awaited_once()
        # Sync variant must NOT be called from the wire path -- that
        # path is reserved for mutate_on_next_pause callbacks.
        assert not system.insert_method.called


class TestPerThreadSerialization:
    """Concurrent mutations on the same thread serialize via an asyncio.Lock.

    Without serialization, two operators calling skip_method simultaneously
    could both pass `_validate_paused` and race into the system layer.
    Different threads still mutate concurrently.
    """

    @pytest.mark.asyncio
    async def test_concurrent_same_thread_mutations_serialize(self) -> None:
        facade, _runtime, system = _make_facade(["t1"])

        in_flight = 0
        max_observed = 0
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_abort(thread_id: str, mid: str | None, mname: str | None) -> None:
            nonlocal in_flight, max_observed
            in_flight += 1
            max_observed = max(max_observed, in_flight)
            entered.set()
            # Hold the section open so a missing per-thread lock has an unbounded
            # window to let a sibling in; the lock keeps every sibling parked.
            await release.wait()
            in_flight -= 1

        system.abort_method = slow_abort

        task = asyncio.gather(
            facade.abort_method("e1", "t1", method_name="m1", reason="r", confirm=True),
            facade.abort_method("e1", "t1", method_name="m2", reason="r", confirm=True),
            facade.abort_method("e1", "t1", method_name="m3", reason="r", confirm=True),
        )
        await entered.wait()
        # Pump the loop (turns, not wall-clock) so the other two drive their
        # lock acquire; a missing lock would let them into slow_abort here.
        for _ in range(20):
            await asyncio.sleep(0)
        assert in_flight == 1, (
            f"Expected serialized execution (max in-flight=1) but observed "
            f"{in_flight} concurrent mutations on the same thread"
        )
        release.set()
        await task

        assert max_observed == 1

    @pytest.mark.asyncio
    async def test_different_threads_run_concurrently(self) -> None:
        facade, _runtime, system = _make_facade(["t1", "t2", "t3"])

        in_flight = 0
        max_observed = 0

        async def slow_abort(thread_id: str, mid: str | None, mname: str | None) -> None:
            nonlocal in_flight, max_observed
            in_flight += 1
            max_observed = max(max_observed, in_flight)
            # Hold until all three have been concurrently in-flight; max_observed
            # is monotonic so every waiter sees the peak (in_flight decrements).
            await wait_until(lambda: max_observed >= 3, timeout=5.0)
            in_flight -= 1

        system.abort_method = slow_abort

        await asyncio.gather(
            facade.abort_method("e1", "t1", method_name="m", reason="r", confirm=True),
            facade.abort_method("e1", "t2", method_name="m", reason="r", confirm=True),
            facade.abort_method("e1", "t3", method_name="m", reason="r", confirm=True),
        )

        assert max_observed == 3, (
            f"Different threads should mutate concurrently but observed "
            f"max in-flight={max_observed} (expected 3)"
        )


class TestTypedMutationErrors:
    """Wire callers map mutation precondition failures to clean error codes.

    The MutationCoordinator raises typed subclasses of MutationError so
    REST/MCP error envelopes can use stable error codes instead of brittle
    string-match-on-ValueError-message.
    """

    def test_typed_errors_importable(self) -> None:
        from orca.system.mutation.errors import (
            MutationError,
            ThreadNotPausedError,
            MethodNotAssignedError,
            MethodNotInProgressError,
            ActionAlreadyExecutingError,
            MethodAlreadyInProgressError,
        )

        # Every concrete error inherits ValueError (so existing
        # `except ValueError` callers still catch them) and MutationError
        # (so wire layers can catch the family with one except clause).
        for cls in (
            ThreadNotPausedError, MethodNotAssignedError,
            MethodNotInProgressError, ActionAlreadyExecutingError,
            MethodAlreadyInProgressError,
        ):
            assert issubclass(cls, MutationError)
            assert issubclass(cls, ValueError)

    def test_thread_not_paused_carries_status(self) -> None:
        from orca.system.mutation.errors import ThreadNotPausedError

        err = ThreadNotPausedError(
            thread_name="plate_a-1234", current_status="EXECUTING_ACTION",
        )
        assert err.thread_name == "plate_a-1234"
        assert err.current_status == "EXECUTING_ACTION"
        assert err.terminal is False
        assert "EXECUTING_ACTION" in str(err)
        assert "PAUSED" in str(err)

    def test_thread_not_paused_terminal_flavor(self) -> None:
        """COMPLETED / STOPPED threads get a different message but the same
        error class so wire layers map to the same error code.
        """
        from orca.system.mutation.errors import ThreadNotPausedError

        err = ThreadNotPausedError(
            thread_name="plate_a-1234", current_status="COMPLETED",
        )
        assert err.terminal is True
        assert "cannot accept mutations" in str(err)

    def test_method_not_in_progress_carries_method(self) -> None:
        from orca.system.mutation.errors import MethodNotInProgressError

        err = MethodNotInProgressError(
            thread_name="plate_a-1234", method_name="incubate",
            current_status="QUEUED",
        )
        assert err.method_name == "incubate"
        assert err.current_status == "QUEUED"


class TestReasonAuditTrail:
    """`reason` flows through @dangerous to the audit ring buffer.

    The audit channel for runtime mutations is the existing
    @dangerous AuditTrail (see src/orca/runtime/danger.py). When a wire
    caller supplies `reason`, it lands in the AuditEntry's reason field
    and (with a configured handler) the orca.audit logger emit. No
    bespoke ops-history wiring is needed — the decorator handles it.
    """

    @pytest.mark.asyncio
    async def test_skip_method_records_reason(self) -> None:
        from orca.runtime.danger import clear_audit_trail, list_audit_entries

        clear_audit_trail()
        facade, _runtime, _system = _make_facade(["t1"])

        await facade.skip_method(
            "exec-1", "t1", method_name="incubate",
            reason="rerunning to capture extra read", confirm=True,
        )

        entries = list_audit_entries()
        skip_entries = [e for e in entries if e.action_name == "thread.skip_method"]
        assert len(skip_entries) == 1
        assert skip_entries[0].reason == "rerunning to capture extra read"

    @pytest.mark.asyncio
    async def test_abort_method_records_reason(self) -> None:
        from orca.runtime.danger import clear_audit_trail, list_audit_entries

        clear_audit_trail()
        facade, _runtime, _system = _make_facade(["t1"])

        await facade.abort_method(
            "exec-1", "t1", method_name="incubate",
            reason="device hung mid-shake", confirm=True,
        )

        entries = list_audit_entries()
        abort_entries = [e for e in entries if e.action_name == "thread.abort_method"]
        assert len(abort_entries) == 1
        assert abort_entries[0].reason == "device hung mid-shake"

    @pytest.mark.asyncio
    async def test_reason_is_required_when_decorator_requires_it(self) -> None:
        """`requires_reason=True` (L4 tightening) on the mutation decorators
        means callers MUST supply reason in addition to confirm=True. The
        @dangerous wrapper raises ValueError before the function body runs;
        the system layer is never touched, so no half-applied mutation can
        slip through audit-less.
        """
        from orca.runtime.danger import clear_audit_trail, list_audit_entries

        clear_audit_trail()
        facade, _runtime, system = _make_facade(["t1"])

        with pytest.raises(ValueError, match="requires reason"):
            await facade.skip_method(
                "exec-1", "t1", method_name="incubate", confirm=True,
            )

        # Mutation never reached the system layer.
        system.skip_pending_method.assert_not_called()
        # And no audit entry was recorded for the rejected call.
        entries = list_audit_entries()
        skip_entries = [e for e in entries if e.action_name == "thread.skip_method"]
        assert skip_entries == []

    @pytest.mark.asyncio
    async def test_replace_action_audits_the_injected_source_not_a_repr(self) -> None:
        """Defect 3 from the 2026-08-28 bench incident: replace_action's
        audit entry recorded a bare repr of the compiled template, so there
        was no way after the fact to answer "what did we actually inject
        into that run". Action.to_dict() now gives @dangerous's JSON-safe
        capture something real to read instead of falling back to repr()."""
        from orca.orca import action as orca_action
        from orca.runtime.danger import clear_audit_trail, list_audit_entries
        from orca.sdk.labware import AnyLabwareTemplate

        clear_audit_trail()
        facade, _runtime, _system = _make_facade(["t1"])
        source = (
            "@orca.action(device=topology.device('shaker_1', IShaker), "
            "inputs=[AnyLabwareTemplate()])\n"
            "async def stamp_col2(ctx):\n"
            "    pass\n"
        )

        @orca_action(device=UniversalMockDevice("shaker_1"), inputs=[AnyLabwareTemplate()])
        async def stamp_col2(ctx: ActionContext) -> None:
            pass

        stamp_col2.injected_source = source

        await facade.replace_action(
            "exec-1", "t1", target_command="stamp", template=stamp_col2,
            reason="rack column 1 is spent", confirm=True,
        )

        entries = list_audit_entries()
        replace_entries = [e for e in entries if e.action_name == "thread.replace_action"]
        assert len(replace_entries) == 1
        recorded_template = replace_entries[0].call_args["template"]
        assert recorded_template == {"name": "stamp_col2", "injected_source": source}
        assert not isinstance(recorded_template, str), (
            "must be a structured JSON-safe dict, not a repr() string"
        )
