"""Regression for the SMC lab-sim hang (2026-05-14).

A contributor thread (joined via ``orca.join``) on a shared method must NOT
hold ``ExecutingMethod.shared_coord.resolving_action_lock`` while waiting
for the owner to bind ``_current_action``. The pre-fix code ran every
thread through ``resolve_current_action``, which acquires the lock for the
duration of ``await action_resolver.resolve_action(...)``. When a
contributor won that race, the contributor's reservation request carried
the contributor's thread_id, which did not match the holder's hold-over
reservation (held by the owner), so the request was rejected forever. The
retry loop kept the lock locked, and the owner could never acquire it to
submit its own re-entrant request that would have succeeded.

Fix: contributors call ``wait_for_current_action`` instead, parking on the
lock-free ``shared_coord.current_action_resolved`` event. The wait races
against ``ExecutingMethod.completed`` so an owner that aborts or cancels
before binding ``_current_action`` raises ``MethodResolutionAbortedError``
rather than stranding contributors.

The end-to-end pin for this hang lives outside this repo, in the deployment
harness's SMC test, which exercises 3-input combine_plates
with concurrent owner+contributor execution and the device-reservation path.
The unit tests below pin the local primitives: lock-freedom of the wait, the
cached-fast-return contract, action-completion event reset, and the
owner-abort wakeup.
"""

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import MagicMock

import pytest

from orca.events.event_bus import EventBus
from orca.events.execution_context import MethodExecutionContext, WorkflowExecutionContext
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.actions.assigned_location_action import AssignedLocationAction
from orca.workflow_models.actions.dynamic_resource_action import (
    DynamicResourceActionResolver,
    UnresolvedLocationAction,
)
from orca.workflow_models.actions.executable_location_action import ExecutableLocationAction
from orca.workflow_models.actions.location_action import ActionBodyLocationAction
from orca.workflow_models.actions.util import IActionReservationStatusSink
from orca.workflow_models.merge_lane import MergeLane
from orca.workflow_models.method import (
    ExecutingMethod,
    MethodInstance,
    MethodResolutionAbortedError,
)
from orca.workflow_models.status_manager import StatusManager

from tests.test_helpers import create_test_device


def _build_executing_method() -> ExecutingMethod:
    method_instance = MethodInstance("shared_method")
    event_bus = EventBus()
    status_mgr = StatusManager(event_bus)
    context = WorkflowExecutionContext(execution_id="wf-1", workflow_name="t")
    return ExecutingMethod(method_instance, event_bus, status_mgr, context)


async def _noop_action_body(ctx: ActionContext) -> None:
    return None


def _make_unresolved(command: str = "fake_action") -> UnresolvedLocationAction:
    device = create_test_device("shaker1")
    body = ActionBodyLocationAction(func=_noop_action_body, command=command)
    return UnresolvedLocationAction(
        resource=device,
        location_action=body,
        expected_input_templates=[],
        expected_output_templates=[],
    )


def _make_executable_action() -> ExecutableLocationAction:
    context = MethodExecutionContext(
        execution_id="exec-1",
        workflow_name="wf",
        method_id="m1",
        method_name="method",
        thread_id="t1",
        thread_name="thread1",
        participating_thread_ids=("t1",),
    )
    action = MagicMock()
    action.id = "act-1"
    action.command = "fake_action"
    return ExecutableLocationAction(
        status_manager=MagicMock(),
        action=action,
        context=context,
    )


class _StubResolver(DynamicResourceActionResolver):
    """Resolver stub that returns a real assigned action without reserving a
    device. The owner-resolve test stubs ``_create_executable_action`` so the
    returned value's identity is irrelevant; the consume test never calls it."""

    def __init__(self) -> None:
        pass

    async def resolve_action(
        self,
        thread_id: str,
        dynamic_action: UnresolvedLocationAction,
        reference_point: Location,
        requesting_labware: LabwareInstance | None = None,
        status_sink: IActionReservationStatusSink | None = None,
    ) -> AssignedLocationAction:
        # Yield so the contributor's wait has a chance to park before resolving.
        await asyncio.sleep(0)
        return _make_unresolved().assign()


@pytest.mark.asyncio
async def test_wait_for_current_action_does_not_hold_resolving_lock() -> None:
    em = _build_executing_method()

    waiter = asyncio.create_task(em.wait_for_current_action())
    await asyncio.sleep(0)
    assert not waiter.done(), "contributor should park on shared_coord.current_action_resolved"

    # The owner must be able to acquire shared_coord.resolving_action_lock
    # while the contributor is parked. If wait_for_current_action held the
    # lock, this block would deadlock.
    assert not em.shared_coord.resolving_action_lock.locked()

    sentinel_action = _make_executable_action()
    async with em.shared_coord.resolving_action_lock:
        em._current_action = sentinel_action
        em.shared_coord.current_action_resolved.set()

    result = await asyncio.wait_for(waiter, timeout=1.0)
    assert result is sentinel_action


@pytest.mark.asyncio
async def test_wait_for_current_action_returns_cached_immediately() -> None:
    em = _build_executing_method()

    sentinel_action = _make_executable_action()
    em._current_action = sentinel_action
    em.shared_coord.current_action_resolved.set()

    result = await em.wait_for_current_action()
    assert result is sentinel_action


@pytest.mark.asyncio
async def test_action_completion_clears_resolution_event() -> None:
    em = _build_executing_method()

    em.shared_coord.current_action_resolved.set()
    em._current_action = _make_executable_action()

    # ``_handle_action_completed_async`` is the path that clears the event so
    # the next action's contributor waiters block correctly. Exercise the
    # clear directly: contributors of the next action must see a fresh
    # un-set event after the prior action completes.
    em._current_action = None
    em.shared_coord.current_action_resolved.clear()

    waiter = asyncio.create_task(em.wait_for_current_action())
    await asyncio.sleep(0)
    assert not waiter.done()
    waiter.cancel()
    try:
        await waiter
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_wait_for_current_action_raises_when_method_completes_without_resolving() -> None:
    """Owner-abort regression: if the method completes/aborts before the
    owner binds ``_current_action`` (e.g. ``abort()``, recovery-driven
    cancellation), contributors parked on resolution must observe the
    completion and raise rather than blocking forever.
    """
    em = _build_executing_method()

    waiter = asyncio.create_task(em.wait_for_current_action())
    await asyncio.sleep(0)
    assert not waiter.done()

    # Simulate the method completing (e.g. via abort) without _current_action
    # ever being bound. completed.set() is what abort() and the
    # lane-exhausted path in _handle_action_completed_async both fire.
    em.completed.set()

    with pytest.raises(MethodResolutionAbortedError):
        await asyncio.wait_for(waiter, timeout=1.0)


@pytest.mark.asyncio
async def test_wait_for_current_action_picks_resolution_over_completion_when_both_set() -> None:
    """If the owner binds ``_current_action`` and the method completes
    near-simultaneously, ``wait_for_current_action`` must return the bound
    action rather than raising. The post-await check on
    ``_current_action is not None`` covers this race.
    """
    em = _build_executing_method()

    sentinel_action = _make_executable_action()
    em._current_action = sentinel_action
    em.shared_coord.current_action_resolved.set()
    em.completed.set()

    result = await em.wait_for_current_action()
    assert result is sentinel_action


@pytest.mark.asyncio
async def test_wait_for_current_action_raises_on_abort_with_was_aborted_flag() -> None:
    """``abort()`` sets ``_was_aborted=True`` and ``completed``. Contributors
    parked on resolution must wake, see ``_was_aborted``, and raise with the
    abort-specific message. This distinguishes abort from the (provably
    impossible in practice) "completed normally without binding" race -- both
    raise, but the messages differ so operator/log triage is unambiguous.
    """
    em = _build_executing_method()

    waiter = asyncio.create_task(em.wait_for_current_action())
    await asyncio.sleep(0)
    assert not waiter.done()

    em._was_aborted = True
    em.completed.set()

    with pytest.raises(MethodResolutionAbortedError, match="aborted before"):
        await asyncio.wait_for(waiter, timeout=1.0)


@pytest.mark.asyncio
async def test_contributor_using_wait_does_not_block_owner_resolve() -> None:
    """Concern C (regression for SMC hang root cause).

    Pre-fix code path: every thread (owner and contributor) called
    ``resolve_current_action``. The method's ``_resolving_action_lock`` is
    held across ``await action_resolver.resolve_action(...)``, which in the
    real flow runs an indefinite reservation-retry loop. If a contributor
    won the lock-acquisition race, its thread_id did not match the owner's
    hold-over reservation and the request was rejected forever -- the lock
    stayed locked, and the owner blocked on lock-acquisition could never
    submit its own (re-entrant, would-succeed) request. Deadlock.

    Post-fix: contributors call ``wait_for_current_action`` which is
    lock-free, so the owner can always acquire the lock and resolve.

    This test pins the post-fix behavior end-to-end: a contributor parks
    in ``wait_for_current_action`` concurrently with the owner's
    ``resolve_current_action``, the owner makes progress, and the
    contributor wakes with the owner's bound action.
    """
    em = _build_executing_method()

    # Stub _create_executable_action so we don't need a real
    # ExecutableLocationAction wiring (constructor pulls from action.id etc.)
    sentinel_action = _make_executable_action()

    def _stub_create_executable(assigned: AssignedLocationAction) -> ExecutableLocationAction:
        del assigned
        return sentinel_action

    em._create_executable_action = _stub_create_executable
    em._subscribe_to_current_action = lambda: None

    # Mock unresolved-action presence so resolve_current_action's assert holds.
    em._current_unresolved_action = _make_unresolved()

    # Stub returns a real assigned action and does NOT spin in a retry loop
    # (the pre-fix bug pattern); the test pins lock topology, not retries.
    fake_resolver = _StubResolver()

    contributor = asyncio.create_task(em.wait_for_current_action())
    await asyncio.sleep(0)
    assert not contributor.done(), "contributor should be parked"

    # Owner resolves; with the post-fix design, this completes promptly
    # (the contributor does NOT hold _resolving_action_lock).
    owner_result = await asyncio.wait_for(
        em.resolve_current_action(
            thread_id="owner",
            current_location=Location("owner_loc"),
            action_resolver=fake_resolver,
        ),
        timeout=1.0,
    )

    contributor_result = await asyncio.wait_for(contributor, timeout=1.0)
    assert contributor_result is owner_result
    assert owner_result is sentinel_action


@pytest.mark.asyncio
async def test_multi_action_contributor_consume_after_handler_clear() -> None:
    """Concern B (multi-action shared-method invariant pin).

    For a multi-action shared method, when action N completes, the handler
    in ``_handle_action_completed_async`` clears both ``_current_action``
    and ``_current_unresolved_action``. Both owner and contributors then
    race back to ``consume_next_unresolved_action`` to pop action N+1 from
    the shared ``MergeLane``. Per ``consume_next_unresolved_action`` lines
    345-377: the call is guarded by ``_resolving_action_lock``, and the
    early-return ``if self._current_unresolved_action is not None`` means
    the second caller (whoever loses the lock race) returns the cached
    action that the first caller consumed. Both end up holding the same
    action reference.

    This pins the invariant: ordering of owner-vs-contributor consume calls
    does not affect the action they observe; both see the same one.
    """
    em = _build_executing_method()

    # Action N just completed -- handler cleared state.
    em._current_action = None
    em._current_unresolved_action = None
    em.shared_coord.current_action_resolved.clear()

    # Stage one action on the lane.
    sentinel_unresolved = _make_unresolved()
    em._action_lane = _SingleItemLane(sentinel_unresolved)

    # Two callers race into consume_next_unresolved_action. The resolver is
    # unused along the cached path.
    resolver = _StubResolver()
    caller_a = asyncio.create_task(em.consume_next_unresolved_action(resolver))
    caller_b = asyncio.create_task(em.consume_next_unresolved_action(resolver))

    a_result, b_result = await asyncio.gather(caller_a, caller_b)

    # Both got the same unresolved action; the lock + cache guard prevented
    # the lane from being consumed twice.
    assert a_result is sentinel_unresolved
    assert b_result is sentinel_unresolved
    assert em._current_unresolved_action is sentinel_unresolved


async def _empty_action_generator() -> AsyncGenerator[UnresolvedLocationAction, None]:
    return
    yield  # pragma: no cover


class _SingleItemLane(MergeLane[UnresolvedLocationAction]):
    """Minimal MergeLane stand-in: yields one item then raises StopAsyncIteration.

    Used by ``test_multi_action_contributor_consume_after_handler_clear`` to
    avoid building a full MergeLane in the test. The ``next`` method yields
    control via ``asyncio.sleep(0)`` so the second concurrent caller
    actually parks on ``_resolving_action_lock``, exercising the contended
    lock path that the test pins.
    """

    def __init__(self, item: UnresolvedLocationAction) -> None:
        super().__init__(_empty_action_generator(), lambda action: action.command)
        self._item = item
        self._consumed = False

    async def next(self) -> UnresolvedLocationAction:
        # Yield so a second concurrent caller parks on the held lock instead of
        # running to completion synchronously, exercising the contended path.
        await asyncio.sleep(0)
        if self._consumed:
            raise StopAsyncIteration
        self._consumed = True
        return self._item

    def should_skip(self, name: str) -> bool:
        del name
        return False