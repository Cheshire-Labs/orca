"""Regression tests for the receiver_drained slot flag.

The flag distinguishes "active receiver still suspended at a join, can
consume more contributions" from "receiver's user generator has returned,
thread is moving toward end_location but not yet COMPLETED."

Without the flag, slot.has_active_thread() returns True throughout that
moving-home window, so concurrent auto-spawn callbacks bind new methods'
input slots to the leaving thread's labware instance. The labware is then
physically picked off the device and the action body fails with
"Missing labware ..." under default FailurePolicy.PAUSE.

These tests pin:
- LabwareSlot.has_active_thread() returns False when receiver_drained.
- drain_for_handoff resets receiver_drained for the next receiver.
- The auto-spawn callback respects the flag and routes to the
  fresh-receiver branch instead of binding a leaving thread.
- The capacity-precheck callback respects the flag the same way (P2 was
  audited as sharing the root cause with the auto-spawn site).
"""
from typing import Awaitable, Callable, cast
from unittest.mock import MagicMock

import pytest

from orca.events.event_bus import EventBus
from orca.events.execution_context import ExecutionContext
from orca.resource_models.capacity import CapacityPolicy, OverflowAction
from orca.runtime.run_modes import WorkflowRunMode
from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.state.records import DeclaredTracking
from orca.resource_models.labware_state import (
    InMemoryLabwareRegistry,
    LabwareSlot,
    SLOT_CLOSED_SENTINEL,
)
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.method import ExecutingMethod
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflows.executing_workflow import (
    CreateThreadFn,
    ExecutingWorkflow,
)
from orca.workflow_models.workflows.workflow import WorkflowInstance
from orca.workflow_models.workflow_templates import WorkflowTemplate


# ---------------------------------------------------------------------------
# Test doubles (kept self-contained; mirror test_capacity_spawn_callback)
# ---------------------------------------------------------------------------

class _Labware(LabwareInstance):
    """Minimal LabwareInstance with always-True can_continue."""

    def __init__(self, name: str) -> None:
        super().__init__(name, "test_type")

    async def can_continue(self, demand: DeclaredTracking | None = None) -> bool:
        return True


class _StubLabwareTemplate(LabwareTemplate):
    def __init__(self, name: str, labware: LabwareInstance) -> None:
        super().__init__(name)
        self._instance = labware

    async def create_instance(self) -> LabwareInstance:
        return self._instance


class _StubRegisteredThread:
    """IRegisteredThread test double: not yet completed."""

    def __init__(self, labware: LabwareInstance, labware_template: LabwareTemplate,
                 thread_instance: LabwareThreadInstance) -> None:
        self._labware_template = labware_template
        self._thread_instance = thread_instance
        self.completed = False

    def has_completed(self) -> bool:
        return self.completed

    def stop(self) -> None:
        self.completed = True

    @property
    def labware_template(self) -> LabwareTemplate | None:
        return self._labware_template

    @property
    def thread_instance(self) -> LabwareThreadInstance:
        return self._thread_instance


# ---------------------------------------------------------------------------
# LabwareSlot unit tests (the building block)
# ---------------------------------------------------------------------------

class TestLabwareSlotReceiverDrained:

    def test_default_receiver_drained_is_false(self) -> None:
        slot = LabwareSlot(slot_key="tips", labware_template_name="tips")
        assert slot.receiver_drained is False

    def test_has_active_thread_false_when_drained(self) -> None:
        """has_active_thread() respects the drained flag even when an
        active_thread is set and not yet has_completed."""
        labware = _Labware("tips")
        template = _StubLabwareTemplate("tips", labware)
        thread_inst = MagicMock(spec=LabwareThreadInstance)
        thread_inst.labware = labware
        active = _StubRegisteredThread(labware, template, thread_inst)

        slot = LabwareSlot(slot_key="tips", labware_template_name="tips")
        slot.active_thread = active

        assert slot.has_active_thread() is True

        slot.receiver_drained = True
        assert slot.has_active_thread() is False

    @pytest.mark.asyncio
    async def test_drain_for_handoff_resets_receiver_drained(self) -> None:
        """drain_for_handoff clears receiver_drained so the next receiver
        starts in the un-drained state."""
        slot = LabwareSlot(slot_key="tips", labware_template_name="tips")
        slot.receiver_drained = True

        await slot.drain_for_handoff(callback=None, run_mode=WorkflowRunMode.PURE_SIM)

        assert slot.receiver_drained is False

    @pytest.mark.asyncio
    async def test_drain_for_handoff_resets_even_with_queued_items(self) -> None:
        """The reset is unconditional: queue items and active-thread don't
        block the flag from clearing."""
        slot = LabwareSlot(slot_key="tips", labware_template_name="tips")
        slot.receiver_drained = True
        method = MagicMock(spec=ExecutingMethod)
        method.name = "m"
        # ``ExecutingMethod.completed`` is an asyncio.Event set in __init__
        # so spec=ExecutingMethod does NOT auto-provide it. drain_for_handoff
        # consults ``.completed.is_set()`` to skip already-completed methods;
        # the mock must expose the attribute and return False here so the
        # method is routed (not skipped).
        method.completed = MagicMock()
        method.completed.is_set.return_value = False
        slot.queue.put_nowait(method)
        # Also drop the close sentinel just to verify drain handles it.
        slot.queue.put_nowait(SLOT_CLOSED_SENTINEL)

        recorded: list[tuple[str, ExecutingMethod]] = []
        async def callback(name: str, m: ExecutingMethod, run_mode: WorkflowRunMode) -> None:
            recorded.append((name, m))

        routed = await slot.drain_for_handoff(callback=callback, run_mode=WorkflowRunMode.PURE_SIM)

        assert slot.receiver_drained is False
        assert routed == 1
        assert recorded == [("tips", method)]

    @pytest.mark.asyncio
    async def test_drain_for_handoff_skips_completed_methods(self) -> None:
        """Bug 3: drain_for_handoff must NOT route already-completed methods.

        The PLR ``test_plr_labware_journeys`` failure shape:
        - Owner runs cherry_pick_step, then dilute_step at the same device.
        - A single-yield tips receiver joins cherry_pick.
        - While that receiver is still processing cherry_pick, the owner's
          ``_auto_spawn_for_action`` for dilute_step sees ``has_active_thread()
          == True`` and queues dilute_step on the receiver's slot.
        - dilute_step's action body completes on the owner side because the
          receiver's labware is still physically at the device.
        - The receiver's user generator exhausts (single yield), thread moves
          to its end_location, then drain_for_handoff runs.
        - PRE-FIX: dilute_step (already completed) gets re-routed to a fresh
          tips receiver via the spawn callback. That fresh receiver immediately
          exits its method loop (``_assigned_method.completed.is_set() == True``)
          without ever moving to the device, breaking the journey assertion.
        - POST-FIX: completed methods are skipped; no spurious spawn.
        """
        slot = LabwareSlot(slot_key="tips", labware_template_name="tips")
        completed_method = MagicMock(spec=ExecutingMethod)
        completed_method.name = "completed_one"
        completed_method.completed = MagicMock()
        completed_method.completed.is_set.return_value = True
        pending_method = MagicMock(spec=ExecutingMethod)
        pending_method.name = "pending_one"
        pending_method.completed = MagicMock()
        pending_method.completed.is_set.return_value = False
        slot.queue.put_nowait(completed_method)
        slot.queue.put_nowait(pending_method)

        recorded: list[tuple[str, ExecutingMethod]] = []
        async def callback(name: str, m: ExecutingMethod, run_mode: WorkflowRunMode) -> None:
            recorded.append((name, m))

        routed = await slot.drain_for_handoff(callback=callback, run_mode=WorkflowRunMode.PURE_SIM)

        # Only the non-completed method is routed.
        assert routed == 1
        assert recorded == [("tips", pending_method)]

    @pytest.mark.asyncio
    async def test_drain_for_handoff_skips_completed_methods_in_pending(self) -> None:
        """Same skip-completed contract applies to the overflow `pending` deque.

        Methods that overflowed the queue capacity sit in slot.pending; if any
        of them completed while waiting (e.g., the owner ran the action with
        a contributor's labware still in place at the device), drain_for_handoff
        must not re-route them.
        """
        slot = LabwareSlot(slot_key="tips", labware_template_name="tips")
        completed = MagicMock(spec=ExecutingMethod)
        completed.name = "completed_one"
        completed.completed = MagicMock()
        completed.completed.is_set.return_value = True
        pending = MagicMock(spec=ExecutingMethod)
        pending.name = "pending_one"
        pending.completed = MagicMock()
        pending.completed.is_set.return_value = False
        slot.pending.append(completed)
        slot.pending.append(pending)

        recorded: list[tuple[str, ExecutingMethod]] = []
        async def callback(name: str, m: ExecutingMethod, run_mode: WorkflowRunMode) -> None:
            recorded.append((name, m))

        routed = await slot.drain_for_handoff(callback=callback, run_mode=WorkflowRunMode.PURE_SIM)

        assert routed == 1
        assert recorded == [("tips", pending)]

    @pytest.mark.asyncio
    async def test_drain_for_handoff_mixed_containers_preserves_order(self) -> None:
        """pending-first then queue: ordering is preserved across both
        containers when filtering completed methods.

        Pins the contract: drain pulls pending FIRST (in deque order),
        THEN queue (FIFO). Completed entries are skipped in-place; their
        absence does not shift surviving entries between containers.
        """
        slot = LabwareSlot(slot_key="tips", labware_template_name="tips")

        def _method(name: str, completed: bool) -> MagicMock:
            m = MagicMock(spec=ExecutingMethod)
            m.name = name
            m.completed = MagicMock()
            m.completed.is_set.return_value = completed
            return m

        completed_p = _method("completed_pending", True)
        live_p = _method("live_pending", False)
        completed_q = _method("completed_queue", True)
        live_q = _method("live_queue", False)

        slot.pending.append(completed_p)
        slot.pending.append(live_p)
        slot.queue.put_nowait(completed_q)
        slot.queue.put_nowait(live_q)

        recorded: list[tuple[str, ExecutingMethod]] = []
        async def callback(name: str, m: ExecutingMethod, run_mode: WorkflowRunMode) -> None:
            recorded.append((name, m))

        routed = await slot.drain_for_handoff(callback=callback, run_mode=WorkflowRunMode.PURE_SIM)

        # pending-first then queue, completed entries skipped:
        assert routed == 2
        assert recorded == [("tips", live_p), ("tips", live_q)]


# ---------------------------------------------------------------------------
# Auto-spawn callback respects receiver_drained
# ---------------------------------------------------------------------------

def _build_workflow_with_slot_and_active_receiver(
    receiver_labware: LabwareInstance,
    capacity: CapacityPolicy | None = None,
) -> tuple[ExecutingWorkflow, InMemoryLabwareRegistry, ThreadTemplate,
           _StubRegisteredThread,
           Callable[[str, ExecutingMethod], Awaitable[None]]]:
    """Build a workflow + pre-populated slot with an active receiver bound.

    Mirrors the harness in test_capacity_spawn_callback so the regression
    tests stay close to the existing exercise of the spawn callback.
    """
    receiver_template = _StubLabwareTemplate(
        receiver_labware.template_name, receiver_labware,
    )
    thread_template = MagicMock(spec=ThreadTemplate)
    thread_template.name = f"{receiver_labware.template_name}_thread"
    thread_template.labware_template = receiver_template
    # MagicMock(spec=ThreadTemplate) auto-creates `start_reuse_existing` as
    # a truthy MagicMock, which would trip the reuse-bind branch in the
    # auto-spawn callback. Explicit False keeps this test on the default
    # create-fresh path it exercises.
    thread_template.start_reuse_existing = False

    template = WorkflowTemplate(name="test_workflow")
    template.register_auto_spawn(thread_template, capacity=capacity)

    workflow_instance = WorkflowInstance(name="test_workflow", template=template)
    registry = InMemoryLabwareRegistry()

    create_thread_fn = cast(CreateThreadFn, MagicMock())

    workflow = ExecutingWorkflow(
        workflow=workflow_instance,
        thread_reservation_coordinator=MagicMock(),
        system_thread_manager=MagicMock(),
        event_bus=EventBus(),
        move_handler=MagicMock(),
        status_manager=MagicMock(),
        system_map=MagicMock(),
        create_thread_fn=create_thread_fn,
        labware_registry=registry,
    )

    callback = workflow._auto_spawn_callback
    assert callback is not None

    receiver_thread_inst = MagicMock(spec=LabwareThreadInstance)
    receiver_thread_inst.labware = receiver_labware

    active = _StubRegisteredThread(
        labware=receiver_labware,
        labware_template=receiver_template,
        thread_instance=receiver_thread_inst,
    )
    slot = registry.get_or_create_slot(receiver_template.name, receiver_template.name)
    slot.active_thread = active
    slot.policy = capacity

    return workflow, registry, thread_template, active, callback


class TestSpawnCallbackHonorsReceiverDrained:

    @pytest.mark.asyncio
    async def test_drained_slot_bypasses_active_branch(self) -> None:
        """When receiver_drained is set, the auto-spawn callback must NOT
        bind the new method to the slot's existing active_thread.

        Pinned via the `assign_thread` mock: with the bug, the callback
        would call shared_method.assign_thread(template, leaving_labware)
        before queuing. With the fix, it goes to the fresh-receiver branch,
        which calls create_thread_fn (we mocked it; calls are recorded).
        """
        labware = _Labware("plate_recv")
        workflow, registry, thread_template, active, callback = (
            _build_workflow_with_slot_and_active_receiver(labware)
        )
        slot = registry.get_slot(thread_template.labware_template.name)
        assert slot is not None
        slot.receiver_drained = True

        method = MagicMock(spec=ExecutingMethod)
        method.name = "next_method"
        method.aggregate_demand = MagicMock(return_value=None)
        method.contributor_context = None

        # Sanity: with the active branch, .assign_thread would be called
        # with the active thread's labware. With the fix, the fresh branch
        # creates a new thread instead. We assert via create_thread_fn:
        # the fresh branch awaits create_thread_fn(template, ...).
        create_fn = cast(MagicMock, workflow._create_thread_fn)

        # Force create_thread_fn to behave as an awaitable returning a
        # mock thread_instance, since the fresh branch awaits it.
        async def fake_create(*args: object, **kwargs: object) -> LabwareThreadInstance:
            inst = MagicMock(spec=LabwareThreadInstance)
            inst.labware = labware
            return cast(LabwareThreadInstance, inst)
        create_fn.side_effect = fake_create
        create_fn.reset_mock()

        # add_thread is also called in the fresh branch.
        added = MagicMock()
        added.manual_start = False
        workflow.add_thread = MagicMock(return_value=added)  # type: ignore[method-assign]
        workflow.start_thread = MagicMock()  # type: ignore[method-assign]

        await callback(thread_template.labware_template.name, method, WorkflowRunMode.PURE_SIM)

        # The fresh-receiver branch must have been taken: create_thread_fn
        # was awaited. If the bug were live (no flag check), the active
        # branch would short-circuit and create_thread_fn would be untouched.
        assert create_fn.called, (
            "auto-spawn callback bound to drained active_thread instead of "
            "spawning a fresh receiver"
        )

    @pytest.mark.asyncio
    async def test_undrained_slot_uses_active_branch(self) -> None:
        """Symmetric pin: when receiver_drained is False (default), the
        callback DOES bind to the active receiver. Guards against an
        accidental flip of the predicate."""
        labware = _Labware("plate_recv")
        workflow, registry, thread_template, active, callback = (
            _build_workflow_with_slot_and_active_receiver(
                labware,
                capacity=CapacityPolicy(max_contributions=2,
                                        overflow_action=OverflowAction.NEW),
            )
        )
        slot = registry.get_slot(thread_template.labware_template.name)
        assert slot is not None
        # receiver_drained intentionally left False (the default).

        method = MagicMock(spec=ExecutingMethod)
        method.name = "next_method"
        method.aggregate_demand = MagicMock(return_value=None)
        method.contributor_context = None

        create_fn = cast(MagicMock, workflow._create_thread_fn)
        create_fn.reset_mock()

        await callback(thread_template.labware_template.name, method, WorkflowRunMode.PURE_SIM)

        # Active branch: queue grew, contributions increment, no fresh spawn.
        assert create_fn.called is False, (
            "auto-spawn fell through to fresh branch even though slot was "
            "undrained and had room"
        )
        assert slot.contributions_to_active == 1

    @pytest.mark.asyncio
    async def test_fresh_receiver_branch_resets_drained_flag(self) -> None:
        """P0-A from review: the fresh-receiver branch must reset
        receiver_drained to False. Otherwise the new receiver inherits the
        stale True flag and every subsequent contributor would spawn yet
        another fresh receiver, cascading into ghost receivers.

        Constructed: slot with an active_thread (not yet has_completed) and
        receiver_drained = True. Fire the spawn callback once (takes the
        fresh-receiver branch). Assert slot.receiver_drained is now False
        and slot.active_thread is the freshly-added thread.
        """
        labware = _Labware("plate_recv")
        workflow, registry, thread_template, active, callback = (
            _build_workflow_with_slot_and_active_receiver(labware)
        )
        slot = registry.get_slot(thread_template.labware_template.name)
        assert slot is not None
        slot.receiver_drained = True
        prior_active = slot.active_thread

        method = MagicMock(spec=ExecutingMethod)
        method.name = "next_method"
        method.aggregate_demand = MagicMock(return_value=None)
        method.contributor_context = None

        create_fn = cast(MagicMock, workflow._create_thread_fn)

        async def fake_create(*args: object, **kwargs: object) -> LabwareThreadInstance:
            inst = MagicMock(spec=LabwareThreadInstance)
            inst.labware = labware
            return cast(LabwareThreadInstance, inst)
        create_fn.side_effect = fake_create

        added = MagicMock()
        added.manual_start = False
        workflow.add_thread = MagicMock(return_value=added)  # type: ignore[method-assign]
        workflow.start_thread = MagicMock()  # type: ignore[method-assign]

        await callback(thread_template.labware_template.name, method, WorkflowRunMode.PURE_SIM)

        # Fresh-receiver branch was taken; the slot must now point at the
        # newly-added thread, and receiver_drained must be reset.
        assert slot.active_thread is added, (
            "fresh-receiver branch did not install the new thread as active"
        )
        assert slot.active_thread is not prior_active, (
            "active_thread still points at the previous (drained) receiver"
        )
        assert slot.receiver_drained is False, (
            "fresh-receiver branch left receiver_drained=True; the new "
            "receiver inherits a stale flag and subsequent contributors "
            "will keep spawning ghost receivers"
        )

    @pytest.mark.asyncio
    async def test_active_branch_revalidates_after_capacity_await(self) -> None:
        """Section-1 race-hole pin: between the has_active_thread() check at
        the top of the active branch and the bind, there's an
        ``await active_lw.can_continue(demand)``. If the receiver drains
        during that await, the bind must NOT proceed to the (now-leaving)
        thread.

        We simulate the race by making can_continue itself flip
        receiver_drained=True on the slot, mid-await. With the re-validation
        fix, the callback must fall through to the fresh-receiver branch.
        Without the fix, it would bind to the leaving thread.
        """
        flag_flipping_lw = _Labware("plate_recv")

        # Build harness, then swap the labware's can_continue for a
        # version that flips receiver_drained mid-await to model the race.
        workflow, registry, thread_template, active, callback = (
            _build_workflow_with_slot_and_active_receiver(flag_flipping_lw)
        )
        slot = registry.get_slot(thread_template.labware_template.name)
        assert slot is not None

        async def race_can_continue(
            demand: DeclaredTracking | None = None,
        ) -> bool:
            # Receiver's adapter would set this synchronously; we model it
            # firing during the await window by mutating before returning.
            slot.receiver_drained = True
            return True
        flag_flipping_lw.can_continue = race_can_continue  # type: ignore[method-assign]

        method = MagicMock(spec=ExecutingMethod)
        method.name = "next_method"
        method.aggregate_demand = MagicMock(return_value=None)
        method.contributor_context = None

        create_fn = cast(MagicMock, workflow._create_thread_fn)

        async def fake_create(*args: object, **kwargs: object) -> LabwareThreadInstance:
            inst = MagicMock(spec=LabwareThreadInstance)
            inst.labware = flag_flipping_lw
            return cast(LabwareThreadInstance, inst)
        create_fn.side_effect = fake_create

        added = MagicMock()
        added.manual_start = False
        workflow.add_thread = MagicMock(return_value=added)  # type: ignore[method-assign]
        workflow.start_thread = MagicMock()  # type: ignore[method-assign]

        await callback(thread_template.labware_template.name, method, WorkflowRunMode.PURE_SIM)

        # Re-validation must have noticed the drain and fallen through to
        # fresh-receiver branch. Symptoms of correct behavior:
        #   - create_thread_fn was called (fresh branch)
        #   - slot.active_thread is now the fresh thread, not the original.
        #   - receiver_drained is False (reset by fresh-receiver branch).
        assert create_fn.called, (
            "active branch bound to the receiver even though it drained "
            "during can_continue() — the re-validation after the await is "
            "missing or insufficient"
        )
        assert slot.active_thread is added
        assert slot.receiver_drained is False
