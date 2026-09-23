"""
Tests for cross-tick deadlock detection.

The reservation system's tick loop snapshots the queue, clears it, and runs
deadlock detection on that snapshot alone. When threads retry on different
ticks, their requests never coexist in a single snapshot, so circular waits
go undetected and the system hangs.

These tests verify that:
1. The detector can find cycles without mutating collections (find_yielding_thread)
2. Rejected collections accumulate in carry across ticks
3. Cross-tick deadlocks are detected from carry and flagged for next submission
4. Same-tick deadlocks still work (regression guard)
5. Full E2E resolution works when threads land on different ticks
"""
import asyncio
import pytest
from collections.abc import AsyncGenerator
from unittest.mock import Mock

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.system.reservation_manager.deadlock_manager import (
    DeadlockStarvationRegistry,
    ThreadDeadlockDetector,
)
from orca.system.reservation_manager.interfaces import IReservationCollection
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.reservation_manager import (
    ThreadReservationCoordinator,
)
from orca.system.reservation_manager.move_handler import (
    MoveActionCollectionReservationRequest,
)
from orca.system.system_map import ILocationRegistry
from orca.system.thread_registry_interface import IThreadRegistry
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.actions.move_action import MoveAction
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_labware(name: str) -> LabwareInstance:
    """Create a LabwareInstance with a known name (id is auto-generated)."""
    return LabwareInstance(name, "plate")


def _make_location(name: str) -> Location:
    """Create a Location backed by an empty PlatePad."""
    pad = PlatePad(name, supports_deadlock_resolution=True)
    return Location(name, pad)


def _place_labware(location: Location, labware: LabwareInstance) -> None:
    """Place labware at a location (synchronous shortcut for tests)."""
    location.resource.initialize_labware(labware)


def _make_mock_thread(labware: LabwareInstance) -> Mock:
    """Create a mock LabwareThreadInstance that owns the given labware."""
    thread = Mock()
    thread.labware = labware
    return thread


def _make_location_registry(locations: dict[str, Location]) -> ILocationRegistry:
    """Create a mock ILocationRegistry that returns the given locations."""
    reg = Mock(spec=ILocationRegistry)
    reg.get_location = Mock(side_effect=lambda name: locations[name])
    reg.locations = list(locations.values())
    # Mocks don't inherit the interface's default sites_of; mirror it.
    reg.sites_of = Mock(side_effect=lambda mutex_key: [
        location for location in locations.values()
        if location.owner_mutex_id == mutex_key
    ])
    return reg


def _make_thread_registry(
    threads: dict[str, Mock],
) -> IThreadRegistry:
    """Create a mock IThreadRegistry returning the given thread mocks."""
    reg = Mock(spec=IThreadRegistry)
    reg.get_thread = Mock(side_effect=lambda tid: threads.get(tid))
    reg.threads = list(threads.values())
    return reg


def _make_move_action(
    labware: LabwareInstance, source: Location, target: Location,
) -> MoveAction:
    """Create a MoveAction with a mock transporter."""
    transporter = Mock()
    transporter.name = "mock_transporter"
    return MoveAction(labware, source, target, transporter)


def _make_collection(
    thread_id: str, labware: LabwareInstance, source: Location, target: Location,
) -> MoveActionCollectionReservationRequest:
    """Create a single-path MoveActionCollectionReservationRequest."""
    move = _make_move_action(labware, source, target)
    return MoveActionCollectionReservationRequest(thread_id, [move])


# ---------------------------------------------------------------------------
# Deadlock scenario: two threads blocking each other
#
#   loc_a has plate_b (owned by thread_b)
#   loc_b has plate_a (owned by thread_a)
#
#   thread_a wants loc_a -> blocked by plate_b -> waits for thread_b
#   thread_b wants loc_b -> blocked by plate_a -> waits for thread_a
#   Cycle: thread_a -> thread_b -> thread_a
# ---------------------------------------------------------------------------

class DeadlockScenario:
    """Reusable two-thread deadlock setup."""

    def __init__(self) -> None:
        self.plate_a = _make_labware("plate_a")
        self.plate_b = _make_labware("plate_b")

        self.loc_a = _make_location("loc_a")
        self.loc_b = _make_location("loc_b")

        # Each location holds the OTHER thread's plate
        _place_labware(self.loc_a, self.plate_b)
        _place_labware(self.loc_b, self.plate_a)

        # Source locations (where each thread currently is)
        self.src_a = _make_location("src_a")
        self.src_b = _make_location("src_b")

        self.thread_a_mock = _make_mock_thread(self.plate_a)
        self.thread_b_mock = _make_mock_thread(self.plate_b)

        self.locations = {
            "loc_a": self.loc_a,
            "loc_b": self.loc_b,
            "src_a": self.src_a,
            "src_b": self.src_b,
        }

        self.threads = {
            "thread_a": self.thread_a_mock,
            "thread_b": self.thread_b_mock,
        }

        self.location_reg = _make_location_registry(self.locations)
        self.thread_reg = _make_thread_registry(self.threads)

    def collection_a(self) -> MoveActionCollectionReservationRequest:
        """Thread A wants loc_a (blocked by plate_b)."""
        return _make_collection("thread_a", self.plate_a, self.src_a, self.loc_a)

    def collection_b(self) -> MoveActionCollectionReservationRequest:
        """Thread B wants loc_b (blocked by plate_a)."""
        return _make_collection("thread_b", self.plate_b, self.src_b, self.loc_b)


# ===========================================================================
# find_yielding_thread (pure detection, no side effects)
# ===========================================================================


class TestFindYieldingThread:

    def test_returns_none_when_no_cycle(self) -> None:
        """Two threads that don't block each other produce no yielding thread."""
        plate_a = _make_labware("plate_a")
        plate_b = _make_labware("plate_b")
        loc_a = _make_location("loc_a")
        loc_b = _make_location("loc_b")
        src = _make_location("src")

        # loc_a is empty, loc_b is empty -> no blocking
        col_a = _make_collection("thread_a", plate_a, src, loc_a)
        col_b = _make_collection("thread_b", plate_b, src, loc_b)
        # Mark both as "rejected" for the detector input
        col_a.rejected.set()
        col_b.rejected.set()

        thread_reg = _make_thread_registry({
            "thread_a": _make_mock_thread(plate_a),
            "thread_b": _make_mock_thread(plate_b),
        })
        starvation = DeadlockStarvationRegistry()
        detector = ThreadDeadlockDetector(thread_reg, starvation, reservation_at=lambda _position_id: None)

        result = detector.find_yielding_thread([col_a, col_b])
        assert result is None

    def test_returns_yielder_on_cycle(self) -> None:
        """Circular dependency returns the yielding thread (lowest starvation)."""
        s = DeadlockScenario()
        starvation = DeadlockStarvationRegistry()
        detector = ThreadDeadlockDetector(s.thread_reg, starvation, reservation_at=lambda _position_id: None)

        col_a = s.collection_a()
        col_b = s.collection_b()

        result = detector.find_yielding_thread([col_a, col_b])
        assert result in ("thread_a", "thread_b")

    def test_does_not_mutate_collections(self) -> None:
        """find_yielding_thread is read-only: no events set on collections."""
        s = DeadlockScenario()
        starvation = DeadlockStarvationRegistry()
        detector = ThreadDeadlockDetector(s.thread_reg, starvation, reservation_at=lambda _position_id: None)

        col_a = s.collection_a()
        col_b = s.collection_b()

        detector.find_yielding_thread([col_a, col_b])

        assert not col_a.deadlocked.is_set()
        assert not col_b.deadlocked.is_set()
        assert not col_a.rejected.is_set()
        assert not col_b.rejected.is_set()


# ===========================================================================
# carry accumulation, cleanup, and cross-tick detection
# ===========================================================================


class TestCarryAccumulation:

    @pytest.mark.asyncio
    async def test_carry_accumulates_rejected_collections(self) -> None:
        """Rejected threads on separate ticks accumulate in carry.

        Uses a non-cyclic scenario (both blocked by external labware, not
        each other) so cross-tick detection doesn't immediately consume them.
        """
        # Two locations occupied by labware that no thread in queue owns
        external_plate_x = _make_labware("external_x")
        external_plate_y = _make_labware("external_y")
        loc_x = _make_location("loc_x")
        loc_y = _make_location("loc_y")
        _place_labware(loc_x, external_plate_x)
        _place_labware(loc_y, external_plate_y)
        src = _make_location("src")

        plate_a = _make_labware("plate_a")
        plate_b = _make_labware("plate_b")

        locations = {"loc_x": loc_x, "loc_y": loc_y, "src": src}
        threads = {
            "thread_a": _make_mock_thread(plate_a),
            "thread_b": _make_mock_thread(plate_b),
        }
        location_reg = _make_location_registry(locations)
        thread_reg = _make_thread_registry(threads)
        coordinator = ThreadReservationCoordinator(location_reg, thread_reg)

        # Tick 1: thread_a wants loc_x (blocked by external labware)
        col_a = _make_collection("thread_a", plate_a, src, loc_x)
        async with coordinator._lock:
            coordinator._queue.append(col_a)
        await coordinator._on_tick()
        assert col_a.rejected.is_set()
        assert "thread_a" in coordinator._deadlock_detector._rejected_carry

        # Tick 2: thread_b wants loc_y (blocked by external labware)
        col_b = _make_collection("thread_b", plate_b, src, loc_y)
        async with coordinator._lock:
            coordinator._queue.append(col_b)
        await coordinator._on_tick()
        assert col_b.rejected.is_set()

        # Carry should have both (no cycle, so no flagging)
        assert "thread_a" in coordinator._deadlock_detector._rejected_carry
        assert "thread_b" in coordinator._deadlock_detector._rejected_carry

    @pytest.mark.asyncio
    async def test_carry_cleaned_on_grant(self) -> None:
        """When a thread is granted, it is removed from carry."""
        s = DeadlockScenario()
        coordinator = ThreadReservationCoordinator(s.location_reg, s.thread_reg)

        # Tick 1: thread_a rejected (loc_a occupied) -> enters carry
        col_a = s.collection_a()
        async with coordinator._lock:
            coordinator._queue.append(col_a)
        await coordinator._on_tick()
        assert "thread_a" in coordinator._deadlock_detector._rejected_carry

        # Clear plate_b from loc_a so thread_a can be granted
        await s.loc_a.resource.notify_picked(s.plate_b, Mock())

        # Tick 2: thread_a resubmits (loc_a now empty) -> granted
        col_a2 = s.collection_a()
        async with coordinator._lock:
            coordinator._queue.append(col_a2)
        await coordinator._on_tick()
        assert col_a2.granted.is_set()

        # Carry should no longer have thread_a
        assert "thread_a" not in coordinator._deadlock_detector._rejected_carry

    @pytest.mark.asyncio
    async def test_carry_cleaned_on_same_tick_deadlock(self) -> None:
        """When a thread is deadlocked (same-tick), it is removed from carry."""
        s = DeadlockScenario()
        coordinator = ThreadReservationCoordinator(s.location_reg, s.thread_reg)

        # Tick 1: thread_a alone (rejected, enters carry)
        col_a = s.collection_a()
        async with coordinator._lock:
            coordinator._queue.append(col_a)
        await coordinator._on_tick()
        assert "thread_a" in coordinator._deadlock_detector._rejected_carry

        # Tick 2: both threads submit together (same-tick deadlock detected)
        col_a2 = s.collection_a()
        col_b = s.collection_b()
        async with coordinator._lock:
            coordinator._queue.append(col_a2)
            coordinator._queue.append(col_b)
        await coordinator._on_tick()

        # One should be deadlocked
        deadlocked = [c for c in [col_a2, col_b] if c.deadlocked.is_set()]
        assert len(deadlocked) == 1

        # The deadlocked thread should be removed from carry
        deadlocked_tid = deadlocked[0].thread_id
        assert deadlocked_tid not in coordinator._deadlock_detector._rejected_carry

    @pytest.mark.asyncio
    async def test_single_rejected_thread_no_false_deadlock(self) -> None:
        """One rejected thread in carry does not produce a false deadlock."""
        s = DeadlockScenario()
        coordinator = ThreadReservationCoordinator(s.location_reg, s.thread_reg)

        # Tick 1: only thread_a (rejected)
        col_a = s.collection_a()
        async with coordinator._lock:
            coordinator._queue.append(col_a)
        await coordinator._on_tick()
        assert col_a.rejected.is_set()
        assert not col_a.deadlocked.is_set()

        # No thread should be flagged for deadlock
        assert len(coordinator._deadlock_detector._deadlocked_threads) == 0


class TestCrossTickDeadlockDetection:

    @pytest.mark.asyncio
    async def test_cross_tick_deadlock_detected(self) -> None:
        """A and B form a cycle across two ticks: one gets flagged."""
        s = DeadlockScenario()
        coordinator = ThreadReservationCoordinator(s.location_reg, s.thread_reg)

        # Tick 1: thread_a alone (rejected)
        col_a = s.collection_a()
        async with coordinator._lock:
            coordinator._queue.append(col_a)
        await coordinator._on_tick()

        # Tick 2: thread_b alone (rejected)
        col_b = s.collection_b()
        async with coordinator._lock:
            coordinator._queue.append(col_b)
        await coordinator._on_tick()

        # One thread should now be flagged for deadlock
        assert len(coordinator._deadlock_detector._deadlocked_threads) == 1
        flagged = next(iter(coordinator._deadlock_detector._deadlocked_threads))
        assert flagged in ("thread_a", "thread_b")

    @pytest.mark.asyncio
    async def test_deadlocked_flag_consumed_on_next_submission(self) -> None:
        """Flagged thread gets deadlocked.set() immediately on its next tick."""
        s = DeadlockScenario()
        coordinator = ThreadReservationCoordinator(s.location_reg, s.thread_reg)

        # Tick 1: thread_a alone (rejected)
        col_a = s.collection_a()
        async with coordinator._lock:
            coordinator._queue.append(col_a)
        await coordinator._on_tick()

        # Tick 2: thread_b alone (rejected) -> cross-tick cycle detected
        col_b = s.collection_b()
        async with coordinator._lock:
            coordinator._queue.append(col_b)
        await coordinator._on_tick()

        flagged_tid = next(iter(coordinator._deadlock_detector._deadlocked_threads))

        # Tick 3: flagged thread resubmits
        if flagged_tid == "thread_a":
            col_flagged = s.collection_a()
        else:
            col_flagged = s.collection_b()

        async with coordinator._lock:
            coordinator._queue.append(col_flagged)
        await coordinator._on_tick()

        # The flagged thread's collection should be immediately deadlocked
        assert col_flagged.deadlocked.is_set()
        assert col_flagged.processed.is_set()

        # Flag should be consumed
        assert len(coordinator._deadlock_detector._deadlocked_threads) == 0

    @pytest.mark.asyncio
    async def test_three_thread_transitive_cycle_across_ticks(self) -> None:
        """A->B->C->A cycle detected even when each submits on a separate tick."""
        plate_a = _make_labware("plate_a")
        plate_b = _make_labware("plate_b")
        plate_c = _make_labware("plate_c")

        loc_a = _make_location("loc_a")  # has plate_c
        loc_b = _make_location("loc_b")  # has plate_a
        loc_c = _make_location("loc_c")  # has plate_b
        src = _make_location("src")

        _place_labware(loc_a, plate_c)  # A wants loc_a, blocked by C's plate
        _place_labware(loc_b, plate_a)  # B wants loc_b, blocked by A's plate
        _place_labware(loc_c, plate_b)  # C wants loc_c, blocked by B's plate

        locations = {
            "loc_a": loc_a, "loc_b": loc_b, "loc_c": loc_c, "src": src,
        }
        threads = {
            "thread_a": _make_mock_thread(plate_a),
            "thread_b": _make_mock_thread(plate_b),
            "thread_c": _make_mock_thread(plate_c),
        }
        location_reg = _make_location_registry(locations)
        thread_reg = _make_thread_registry(threads)

        coordinator = ThreadReservationCoordinator(location_reg, thread_reg)

        # Tick 1: thread_a
        col_a = _make_collection("thread_a", plate_a, src, loc_a)
        async with coordinator._lock:
            coordinator._queue.append(col_a)
        await coordinator._on_tick()

        # Tick 2: thread_b
        col_b = _make_collection("thread_b", plate_b, src, loc_b)
        async with coordinator._lock:
            coordinator._queue.append(col_b)
        await coordinator._on_tick()

        # Tick 3: thread_c
        col_c = _make_collection("thread_c", plate_c, src, loc_c)
        async with coordinator._lock:
            coordinator._queue.append(col_c)
        await coordinator._on_tick()

        # One thread should be flagged
        assert len(coordinator._deadlock_detector._deadlocked_threads) == 1


# ===========================================================================
# same-tick detection still works
# ===========================================================================


class TestSameTickDeadlockRegression:

    @pytest.mark.asyncio
    async def test_same_tick_deadlock_still_works(self) -> None:
        """Two threads in the same tick get immediate deadlock detection."""
        s = DeadlockScenario()
        coordinator = ThreadReservationCoordinator(s.location_reg, s.thread_reg)

        col_a = s.collection_a()
        col_b = s.collection_b()

        async with coordinator._lock:
            coordinator._queue.append(col_a)
            coordinator._queue.append(col_b)

        await coordinator._on_tick()

        # Both should be processed
        assert col_a.processed.is_set()
        assert col_b.processed.is_set()

        # Exactly one should be deadlocked (same-tick, immediate notification)
        deadlocked_count = sum(
            1 for c in [col_a, col_b] if c.deadlocked.is_set()
        )
        assert deadlocked_count == 1

        # The other should be rejected (not deadlocked)
        rejected_count = sum(
            1 for c in [col_a, col_b] if c.rejected.is_set()
        )
        assert rejected_count == 1


# ===========================================================================
# full E2E deadlock resolution across ticks
# ===========================================================================


class TestCrossTickDeadlockE2E:

    @pytest.mark.asyncio
    async def test_cross_tick_deadlock_resolution_e2e(self) -> None:
        """
        Full system E2E: two threads deadlock across ticks and both complete.

        Uses the same topology as test_two_robot_deadlock_resolution but with
        a longer retry interval to force cross-tick submission timing.
        """
        import orca.orca as orca
        from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
        from orca.sdk.workflow import WorkflowTemplate
        from orca.events.event_bus import EventBus
        from orca.config import OrcaConfig, ReservationConfig
        from orca.workflow_models.status_enums import LabwareThreadStatus
        from tests.test_helpers import (
            create_test_transporter,
            create_test_device,
            create_test_plate_template,
            create_simple_system_map,
        )

        # Topology: same as test_two_robot_deadlock_resolution
        start_pad_1 = PlatePad("start_pad_1")
        start_pad_2 = PlatePad("start_pad_2")
        loc_b_pad = PlatePad("loc_b")
        device_a = create_test_device("device_a", "device")
        device_c = create_test_device("device_c", "device")
        parking_pad = PlatePad("parking_pad", supports_deadlock_resolution=True)

        robot1 = create_test_transporter(
            "robot1", ["start_pad_1", "loc_a", "loc_b", "parking_pad"]
        )
        robot2 = create_test_transporter(
            "robot2", ["start_pad_2", "loc_b", "loc_c", "parking_pad"]
        )

        registry, system_map = await create_simple_system_map(
            [robot1, robot2],
            {"loc_a": device_a, "loc_c": device_c},
            {
                "parking_pad": parking_pad,
                "start_pad_1": start_pad_1,
                "start_pad_2": start_pad_2,
                "loc_b": loc_b_pad,
            },
        )

        plate1_template = create_test_plate_template("plate1")
        plate2_template = create_test_plate_template("plate2")

        @orca.action(device=device_a, inputs=[plate1_template])
        async def t1_action_a(ctx: ActionContext) -> None:
            await ctx.device().run_protocol("p.pro", {})

        @orca.action(device=device_c, inputs=[plate1_template])
        async def t1_action_c(ctx: ActionContext) -> None:
            await ctx.device().run_protocol("p.pro", {})

        @orca.action(device=device_c, inputs=[plate2_template])
        async def t2_action_c(ctx: ActionContext) -> None:
            await ctx.device().run_protocol("p.pro", {})

        @orca.action(device=device_a, inputs=[plate2_template])
        async def t2_action_a(ctx: ActionContext) -> None:
            await ctx.device().run_protocol("p.pro", {})

        @orca.method
        async def method_thread1(
            ctx: MethodContext,
        ) -> AsyncGenerator[ActionTemplate, None]:
            yield t1_action_a
            yield t1_action_c

        @orca.method
        async def method_thread2(
            ctx: MethodContext,
        ) -> AsyncGenerator[ActionTemplate, None]:
            yield t2_action_c
            yield t2_action_a

        @orca.thread(labware=plate1_template, start=system_map.get_location("start_pad_1"), end=system_map.resolve_journey_location("loc_c"))
        async def thread1(
            ctx: ThreadContext,
        ) -> AsyncGenerator[IMethodTemplate, None]:
            yield method_thread1

        @orca.thread(labware=plate2_template, start=system_map.get_location("start_pad_2"), end=system_map.resolve_journey_location("loc_a"))
        async def thread2(ctx):
            yield method_thread2

        workflow = WorkflowTemplate("cross_tick_deadlock_test")
        workflow.add_thread(thread1, is_start=True)
        workflow.add_thread(thread2, is_start=True)

        event_bus = EventBus()

        # Use a longer retry interval to increase probability of cross-tick landing
        config = OrcaConfig(reservation=ReservationConfig(retry_interval=0.8))

        builder = SdkToSystemBuilder(
            "Cross-Tick Deadlock Test",
            "E2E test for cross-tick deadlock resolution",
            labwares=[plate1_template, plate2_template],
            resources_registry=registry,
            system_map=system_map,
            workflows=[workflow],
            event_bus=event_bus,
            config=config,
        )
        await builder.bind_labwares()
        system = builder.get_system()

        from orca.runtime.run_modes import WorkflowRunMode, current_run_mode
        # Seed the per-task run-mode ContextVar so device.driver dispatch
        # resolves to the sim driver. Manual workflow-instance setup
        # bypasses WorkflowExecutor.start() which would normally seed.
        current_run_mode.set(WorkflowRunMode.PURE_SIM)
        workflow_instance = await system.create_and_register_workflow_instance(
            workflow, run_mode=WorkflowRunMode.PURE_SIM,
        )
        system.add_workflow(workflow_instance)
        executing_workflow = system.get_executing_workflow(workflow_instance.id)

        try:
            await asyncio.wait_for(executing_workflow.start(), timeout=30.0)
        except asyncio.TimeoutError:
            pytest.fail(
                "Workflow timed out - cross-tick deadlock was not resolved!"
            )

        threads = executing_workflow.thread_manager.threads
        assert len(threads) == 2
        for thread in threads:
            assert thread.status == LabwareThreadStatus.COMPLETED, (
                f"Thread {thread.id} did not complete (status: {thread.status}). "
                "Cross-tick deadlock resolution failed!"
            )
