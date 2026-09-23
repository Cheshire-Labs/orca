"""Unit tests for RecoverableRejectStrategy, OverflowAction.RECOVERABLE_REJECT,
and the action-loop wrapper that pauses contributors on recoverable overflow.

The recoverable variant pauses the contributor for operator decision instead
of failing the workflow. The strategy raises RecoverableCapacityExceededError;
the action-loop wrapper (_auto_spawn_with_recovery) catches it and routes
through the thread's _pause_for_error path.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.events.event_bus import EventBus
from orca.events.event_bus_interface import IEventBus
from orca.events.execution_context import ExecutionContext
from orca.resource_models.capacity import (
    CapacityExceededError,
    CapacityPolicy,
    OverflowAction,
    RecoverableCapacityExceededError,
)
from orca.resource_models.labware_state import LabwareSlot
from orca.workflow_models.actions.dynamic_resource_action import UnresolvedLocationAction
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)
from orca.workflow_models.method import ExecutingMethod
from orca.workflow_models.overflow_strategy import (
    RecoverableRejectStrategy,
    RejectStrategy,
    SequentialStashStrategy,
    select_strategy,
)
from orca.workflow_models.labware_threads.thread_state_machine import ThreadEvent
from orca.workflow_models.status_enums import LabwareThreadStatus, PauseSite, RecoveryDecision


class _StubWorkflowRef:
    def __init__(self, event_bus: IEventBus, wf_id: str = "wf-1",
                 wf_name: str = "test_wf") -> None:
        self._id = wf_id
        self._name = wf_name
        self._event_bus = event_bus

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self._name

    @property
    def event_bus(self) -> IEventBus:
        return self._event_bus


def _make_method(name: str = "test_method") -> ExecutingMethod:
    method = MagicMock(spec=ExecutingMethod)
    method.name = name
    return method


def _capture_events(event_bus: EventBus) -> list[tuple[str, ExecutionContext]]:
    captured: list[tuple[str, ExecutionContext]] = []
    event_bus.subscribe_all(lambda name, ctx: captured.append((name, ctx)))
    return captured


def _slot_with_recoverable_policy(slot_key: str = "plate_z",
                                  max_contributions: int = 1) -> LabwareSlot:
    return LabwareSlot(
        slot_key=slot_key,
        labware_template_name=slot_key,
        policy=CapacityPolicy(
            max_contributions=max_contributions,
            overflow_action=OverflowAction.RECOVERABLE_REJECT,
        ),
    )


class TestRecoverableRejectStrategy:
    """Strategy that pauses the contributor for operator decision."""

    def test_emits_awaiting_decision_event_then_raises(self) -> None:
        """Emits SLOT.{key}.AWAITING_DECISION then raises
        RecoverableCapacityExceededError. Operator UI listens for the event
        to surface the recovery prompt."""
        bus = EventBus()
        captured = _capture_events(bus)
        workflow = _StubWorkflowRef(bus)
        slot = _slot_with_recoverable_policy("plate_z")
        method = _make_method("m1")

        with pytest.raises(RecoverableCapacityExceededError, match="plate_z"):
            RecoverableRejectStrategy().on_overflow(slot, method, workflow)

        event_names = [name for name, _ in captured]
        assert "SLOT.plate_z.AWAITING_DECISION" in event_names

    def test_does_not_append_to_pending(self) -> None:
        """The rejected method is NOT stashed in slot.pending. Pending is
        for SequentialStashStrategy's batch handoff. Recoverable reject
        leaves the contributor holding its own work until the operator
        decides what to do."""
        bus = EventBus()
        workflow = _StubWorkflowRef(bus)
        slot = _slot_with_recoverable_policy()
        method = _make_method("m1")

        with pytest.raises(RecoverableCapacityExceededError):
            RecoverableRejectStrategy().on_overflow(slot, method, workflow)

        assert len(slot.pending) == 0

    def test_recoverable_error_is_capacity_exceeded_subclass(self) -> None:
        """RecoverableCapacityExceededError IS-A CapacityExceededError so
        existing code catching CapacityExceededError still observes the
        recoverable variant."""
        assert issubclass(RecoverableCapacityExceededError, CapacityExceededError)

    def test_error_message_includes_slot_key_and_method_name(self) -> None:
        """Error message identifies the slot and rejected method so the
        operator (and anyone reading logs) can act on it directly."""
        bus = EventBus()
        workflow = _StubWorkflowRef(bus)
        slot = _slot_with_recoverable_policy("tips_384", max_contributions=4)
        method = _make_method("transfer_eluate")

        with pytest.raises(RecoverableCapacityExceededError) as exc_info:
            RecoverableRejectStrategy().on_overflow(slot, method, workflow)

        msg = str(exc_info.value)
        assert "tips_384" in msg
        assert "transfer_eluate" in msg


class TestSelectStrategyForRecoverableReject:
    """Verifies select_strategy() recognizes the new enum value."""

    def test_returns_recoverable_reject_strategy_for_recoverable_action(self) -> None:
        """When the policy declares OverflowAction.RECOVERABLE_REJECT,
        select_strategy() returns a RecoverableRejectStrategy instance."""
        slot = LabwareSlot(
            slot_key="any_slot",
            labware_template_name="any_slot",
            policy=CapacityPolicy(
                max_contributions=2,
                overflow_action=OverflowAction.RECOVERABLE_REJECT,
            ),
        )
        strategy = select_strategy(slot)
        assert isinstance(strategy, RecoverableRejectStrategy)

    def test_other_actions_unchanged(self) -> None:
        """Existing OverflowAction mappings (NEW, REJECT) still work; the
        new enum value did not break the dispatch table."""
        new_slot = LabwareSlot(
            slot_key="a", labware_template_name="a",
            policy=CapacityPolicy(max_contributions=1,
                                  overflow_action=OverflowAction.NEW),
        )
        reject_slot = LabwareSlot(
            slot_key="b", labware_template_name="b",
            policy=CapacityPolicy(max_contributions=1,
                                  overflow_action=OverflowAction.REJECT),
        )
        assert isinstance(select_strategy(new_slot), SequentialStashStrategy)
        assert isinstance(select_strategy(reject_slot), RejectStrategy)


def _make_thread_stub() -> MagicMock:
    """Mock ExecutingLabwareThread shaped just enough to drive the wrapper.

    The wrapper calls self._auto_spawn_for_action(...) and self._pause_for_error(...);
    both are method calls on self that we mock. Real method binding for
    _auto_spawn_with_recovery happens at the test call site so the wrapper's
    own control flow (try/except/while) actually runs.
    """
    thread = MagicMock(spec=ExecutingLabwareThread)
    thread._pause_for_error = AsyncMock()
    thread.name = "test_thread"
    return thread


def _paused_at_spawn_capacity() -> MagicMock:
    """A thread error-paused at the spawn-capacity site, and nothing else.

    The two refusal helpers are the real methods: a ``spec`` mock stubs them
    out, and a test of a refusal that mocks the refusal proves nothing.
    """
    thread = MagicMock(spec=ExecutingLabwareThread)
    thread.name = "test_thread"
    thread.status = LabwareThreadStatus.PAUSED
    thread._last_error = RecoverableCapacityExceededError("at capacity")
    thread._pause_site = PauseSite.SPAWN_CAPACITY
    thread.pause_site = PauseSite.SPAWN_CAPACITY
    thread.is_error_paused = True
    thread._following_peer_action = False
    thread._move_action = None
    thread._shared_action_group = MagicMock(return_value=None)
    thread._resume_event = MagicMock()
    thread._refuse_a_verb_this_site_does_not_honour = (
        lambda decision: ExecutingLabwareThread
        ._refuse_a_verb_this_site_does_not_honour(thread, decision)
    )
    thread._refuse_continue_the_ledger_does_not_back = (
        lambda: ExecutingLabwareThread
        ._refuse_continue_the_ledger_does_not_back(thread)
    )
    return thread


async def _run_wrapper(thread: MagicMock, unresolved: UnresolvedLocationAction) -> None:
    """Invoke the real _auto_spawn_with_recovery method against a mock thread."""
    await ExecutingLabwareThread._auto_spawn_with_recovery(thread, unresolved)


class TestAutoSpawnWithRecovery:
    """The wrapper method that implements the action-loop pause/retry contract.

    Operator-visible contract:
    - First-attempt success: wrapper returns; no pause invoked
    - RecoverableCapacityExceededError + RETRY: spawn re-attempted
    - RecoverableCapacityExceededError + ABORT_THREAD: re-raise (kills thread)

    ABORT_ACTION and ABORT_METHOD never reach the wrapper. There is no bound
    action at this site, so `HONOURED_DECISIONS` refuses them on the operator's
    call and the thread never un-pauses. That guarantee is
    `TestSpawnCapacityRefusesTheActionLevelVerbs` below.

    The wrapper used to enforce it itself, by re-pausing after the decision had
    already arrived. Same intent, and the operator learned their verb did not
    apply only after watching the thread resume and stop again.
    """

    @pytest.mark.asyncio
    async def test_returns_immediately_on_first_attempt_success(self) -> None:
        """Happy path: spawn succeeds on first call; wrapper returns without
        pausing the contributor."""
        thread = _make_thread_stub()
        thread._auto_spawn_for_action = AsyncMock(return_value=None)
        unresolved = MagicMock(spec=UnresolvedLocationAction)

        await _run_wrapper(thread, unresolved)

        thread._auto_spawn_for_action.assert_called_once_with(unresolved)
        thread._pause_for_error.assert_not_called()

    @pytest.mark.asyncio
    async def test_retry_decision_re_attempts_spawn(self) -> None:
        """RecoverableCapacityExceededError + RETRY: spawn is called a second
        time. Models the operator freeing capacity (e.g. via slot.close())
        and choosing to retry."""
        thread = _make_thread_stub()
        thread._auto_spawn_for_action = AsyncMock(side_effect=[
            RecoverableCapacityExceededError("at capacity"),
            None,  # second attempt succeeds
        ])
        thread._pause_for_error = AsyncMock(return_value=RecoveryDecision.RETRY)
        unresolved = MagicMock(spec=UnresolvedLocationAction)

        await _run_wrapper(thread, unresolved)

        assert thread._auto_spawn_for_action.call_count == 2
        thread._pause_for_error.assert_called_once()
        # Wrapper drives the thread back to RESOLVING_ACTION_LOCATION via
        # the ThreadStateMachine before retrying so the resume path
        # observes the contributor in the canonical pre-resolution state.
        thread._fire.assert_any_call(ThreadEvent.RECOVERY_RETRY)

    @pytest.mark.asyncio
    async def test_abort_thread_decision_re_raises(self) -> None:
        """RecoverableCapacityExceededError + ABORT_THREAD: wrapper re-raises
        the original error so the action loop terminates the contributor
        through the existing error-propagation path."""
        thread = _make_thread_stub()
        original_error = RecoverableCapacityExceededError("at capacity")
        thread._auto_spawn_for_action = AsyncMock(side_effect=original_error)
        thread._pause_for_error = AsyncMock(return_value=RecoveryDecision.ABORT_THREAD)
        unresolved = MagicMock(spec=UnresolvedLocationAction)

        with pytest.raises(RecoverableCapacityExceededError) as exc_info:
            await _run_wrapper(thread, unresolved)

        assert exc_info.value is original_error
        thread._auto_spawn_for_action.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_verb_the_site_honours_still_drives_the_wrapper(self) -> None:
        """RETRY after the operator has freed capacity: the spawn is
        re-attempted and succeeds. The refusal in front of this site must not
        stop a decision that does apply from reaching the wrapper."""
        thread = _make_thread_stub()
        thread._auto_spawn_for_action = AsyncMock(side_effect=[
            RecoverableCapacityExceededError("at capacity"),
            None,
        ])
        thread._pause_for_error = AsyncMock(return_value=RecoveryDecision.RETRY)
        unresolved = MagicMock(spec=UnresolvedLocationAction)

        await _run_wrapper(thread, unresolved)

        assert thread._auto_spawn_for_action.call_count == 2
        thread._fire.assert_any_call(ThreadEvent.RECOVERY_RETRY)


class TestSpawnCapacityRefusesTheActionLevelVerbs:
    """An operator picking "skip this action" must not kill the contributor.

    The spawn callback fires before action resolution, so there is no action to
    skip. Demoting ABORT_ACTION to ABORT_THREAD would answer a different
    question than the one asked, which is why it has never been done. It is now
    refused on the call: the operator is told, and the thread stays paused.
    """

    @pytest.mark.parametrize("verb", [
        RecoveryDecision.ABORT_ACTION,
        RecoveryDecision.ABORT_METHOD,
        RecoveryDecision.CONTINUE,
    ])
    def test_the_verb_is_refused_and_the_thread_stays_paused(
        self, verb: RecoveryDecision,
    ) -> None:
        thread = _paused_at_spawn_capacity()

        with pytest.raises(ValueError) as refused:
            ExecutingLabwareThread.resume_with_decision(thread, verb)

        assert verb.name in str(refused.value)
        assert "SPAWN_CAPACITY" in str(refused.value)
        assert "still paused" in str(refused.value)
        # Nothing was delivered, so the thread is still waiting on a decision.
        thread._resume_event.set.assert_not_called()

    @pytest.mark.parametrize("verb", [
        RecoveryDecision.RETRY, RecoveryDecision.ABORT_THREAD,
    ])
    def test_the_two_verbs_that_apply_are_delivered(
        self, verb: RecoveryDecision,
    ) -> None:
        """Negative control: the refusal must not swallow a valid decision."""
        thread = _paused_at_spawn_capacity()

        ExecutingLabwareThread.resume_with_decision(thread, verb)

        assert thread._recovery_decision is verb
        thread._resume_event.set.assert_called_once()
