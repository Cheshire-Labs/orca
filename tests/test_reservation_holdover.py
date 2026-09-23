"""Tests for reservation behavior between consecutive same-device actions.

Reservations release after each action completes. Re-entrant reservations
allow the same thread to immediately re-acquire the location, preventing
other threads from stealing it. This must coexist with the deadlock detector
which needs reservations released to break cross-device cycles.
"""

import asyncio
from typing import AsyncGenerator
from unittest.mock import patch

import pytest

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate, MethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import ThreadTemplate
from orca.runtime.run_modes import WorkflowRunMode
from tests.mock import UniversalMockDevice
from tests.test_helpers import create_test_plate_template, create_test_transporter, execution_outcome, wire_system_map


class TestReservationHoldover:

    @pytest.mark.asyncio
    async def test_same_device_consecutive_actions_reentrant(self) -> None:
        """Two consecutive actions at the same device complete without issues.
        Re-entrant reservations allow the same thread to re-acquire the
        location immediately after release, preventing theft."""
        plate = create_test_plate_template("plate_96")
        device = UniversalMockDevice("shaker_1")
        transporter = create_test_transporter("robot1", ["shaker_1", "pad1"])
        pool = ResourcePool("shaker_1", [device])

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        registry.add_resource_pool(pool)

        @orca.action(device=pool, inputs=[plate])
        async def shake_a1(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.action(device=pool, inputs=[plate])
        async def shake_a2(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=2, speed=300)

        @orca.method
        async def two_shakes(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shake_a1
            yield shake_a2

        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"shaker_1": device}, pads=["pad1"])

        pad_loc = system_map.get_location("pad1")

        @orca.thread(labware=plate, start=pad_loc, end=pad_loc)
        async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield two_shakes

        method = two_shakes
        thread = plate_thread
        workflow = WorkflowTemplate("holdover_test")
        workflow.add_thread(thread, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="",
            labwares=[plate], resources_registry=registry,
            system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        call_log: list[int] = []
        original_shake = device.shake

        async def tracking_shake(duration: int, speed: int) -> None:
            call_log.append(speed)
            await original_shake(duration, speed)

        device.shake = tracking_shake  # type: ignore[method-assign]

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)

        assert status.status == "completed", f"Expected completed, got {status.status}"
        assert call_log == [500, 300], (
            f"Both shakes should execute in order. Got: {call_log}"
        )
        await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_different_device_consecutive_actions_release_reservation(self) -> None:
        """When consecutive actions target different devices, reservation for the
        first device should be released before the second action executes."""
        plate = create_test_plate_template("plate_96")
        device1 = UniversalMockDevice("shaker_1")
        device2 = UniversalMockDevice("shaker_2")
        transporter = create_test_transporter("robot1", ["shaker_1", "shaker_2", "pad1"])

        registry = ResourceRegistry()
        registry.add_resource(device1)
        registry.add_resource(device2)
        registry.add_resource(transporter)
        pool1 = ResourcePool("shaker_1", [device1])
        pool2 = ResourcePool("shaker_2", [device2])
        registry.add_resource_pool(pool1)
        registry.add_resource_pool(pool2)

        @orca.action(device=pool1, inputs=[plate])
        async def shake_b1(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.action(device=pool2, inputs=[plate])
        async def shake_b2(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=2, speed=300)

        @orca.method
        async def two_device_shakes(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shake_b1
            yield shake_b2

        system_map = SystemMap(registry)
        await wire_system_map(
            system_map,
            devices={"shaker_1": device1, "shaker_2": device2},
            pads=["pad1"],
        )

        pad_loc = system_map.get_location("pad1")

        @orca.thread(labware=plate, start=pad_loc, end=pad_loc)
        async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield two_device_shakes

        method = two_device_shakes
        thread = plate_thread
        workflow = WorkflowTemplate("two_device_test")
        workflow.add_thread(thread, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="",
            labwares=[plate], resources_registry=registry,
            system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        # Track order of releases and action executions
        event_log: list[str] = []
        original_release = LocationReservation.release_reservation
        original_shake1 = device1.shake
        original_shake2 = device2.shake

        def tracking_release(self: LocationReservation) -> None:
            event_log.append(f"release:{self.reserved_location.position_id}")
            original_release(self)

        async def tracking_shake1(duration: int, speed: int) -> None:
            event_log.append("execute:shaker_1")
            await original_shake1(duration, speed)

        async def tracking_shake2(duration: int, speed: int) -> None:
            event_log.append("execute:shaker_2")
            await original_shake2(duration, speed)

        device1.shake = tracking_shake1  # type: ignore[method-assign]
        device2.shake = tracking_shake2  # type: ignore[method-assign]

        with patch.object(LocationReservation, "release_reservation", tracking_release):
            await runtime.start()
            submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
            status = await execution_outcome(runtime, submission, timeout=10.0)

        assert status.status == "completed", f"Expected completed, got {status.status}"

        assert "execute:shaker_2" in event_log, (
            f"shaker_2 never executed. Log: {event_log}"
        )

        # Rule-7 pull semantics: the mutex may sit pending-drain until asked;
        # the contract is AVAILABILITY (takeover), not an eager release event.
        executing_workflow = runtime._executions[submission.execution_id].executing_workflow
        assert executing_workflow is not None
        coordinator = executing_workflow._thread_reservation_coordinator
        probe = LocationReservation(system_map.get_location("shaker_1"))
        granted = await coordinator.try_reserve_location("probe-thread", "shaker_1", probe)
        assert granted, (
            "shaker_1 not acquirable after its plate left (drain takeover failed)"
        )
        await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_other_thread_cannot_steal_between_same_device_actions(self) -> None:
        """Thread B should not be able to reserve the device between
        Thread A's two consecutive actions at the same device.
        Thread A's two actions should execute consecutively."""
        device = UniversalMockDevice("shaker_1")
        transporter = create_test_transporter("robot1", ["shaker_1", "pad1", "pad2"])

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("shaker_1", [device])
        registry.add_resource_pool(pool)

        plate_a = create_test_plate_template("plate_a")
        plate_b = create_test_plate_template("plate_b")

        # Thread A: two consecutive shakes at shaker_1
        @orca.action(device=pool, inputs=[plate_a])
        async def shake_c1(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.action(device=pool, inputs=[plate_a])
        async def shake_c2(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=300)

        @orca.method
        async def thread_a_shakes(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shake_c1
            yield shake_c2

        # Thread B: one shake at shaker_1
        @orca.action(device=pool, inputs=[plate_b])
        async def shake_c3(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=100)

        @orca.method
        async def thread_b_shake(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shake_c3

        method_a = thread_a_shakes
        method_b = thread_b_shake

        system_map = SystemMap(registry)
        await wire_system_map(
            system_map, devices={"shaker_1": device}, pads=["pad1", "pad2"],
        )

        pad1_loc = system_map.get_location("pad1")
        pad2_loc = system_map.get_location("pad2")

        @orca.thread(labware=plate_a, start=pad1_loc, end=pad1_loc)
        async def thread_a_tmpl(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield thread_a_shakes

        @orca.thread(labware=plate_b, start=pad2_loc, end=pad2_loc)
        async def thread_b_tmpl(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield thread_b_shake

        thread_a = thread_a_tmpl
        thread_b = thread_b_tmpl

        workflow = WorkflowTemplate("contention_test")
        workflow.add_thread(thread_a, is_start=True)
        workflow.add_thread(thread_b, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="",
            labwares=[plate_a, plate_b], resources_registry=registry,
            system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        # Track which thread holds the device at each action execution
        action_sequence: list[str] = []
        original_shake = device.shake

        async def tracking_shake(duration: int, speed: int) -> None:
            action_sequence.append(f"speed={speed}")
            await original_shake(duration, speed)

        device.shake = tracking_shake  # type: ignore[method-assign]

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=15.0)

        assert status.status == "completed", f"Expected completed, got {status.status}"

        # Thread A's two actions (speed=500 and speed=300) should be consecutive
        assert len(action_sequence) == 3, f"Expected 3 shakes, got {action_sequence}"

        a_indices = [i for i, s in enumerate(action_sequence) if s in ("speed=500", "speed=300")]
        assert len(a_indices) == 2, f"Thread A's actions: {a_indices}"
        assert a_indices[1] - a_indices[0] == 1, (
            f"Thread A's two actions should be consecutive but were at indices "
            f"{a_indices}. Full sequence: {action_sequence}"
        )
        await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_single_action_thread_reaches_its_end_location(self) -> None:
        """A one-action thread finishes and lands on its end location.

        Named for what it asserts. It does NOT prove the device is given up
        before the end move, despite being the obvious place to look for that:
        the plate here is the only thing on the device, so teardown would clear
        the reservation either way. That guarantee is pinned in
        ``test_a_departed_owner_releases_the_device.py``.
        """
        plate = create_test_plate_template("plate_96")
        device = UniversalMockDevice("shaker_1")
        transporter = create_test_transporter("robot1", ["shaker_1", "pad1"])
        pool = ResourcePool("shaker_1", [device])

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        registry.add_resource_pool(pool)

        @orca.action(device=pool, inputs=[plate])
        async def shake_d1(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.method
        async def one_shake(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shake_d1

        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"shaker_1": device}, pads=["pad1"])

        pad_loc = system_map.get_location("pad1")

        @orca.thread(labware=plate, start=pad_loc, end=pad_loc)
        async def plate_thread_d(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield one_shake

        method = one_shake
        thread = plate_thread_d
        workflow = WorkflowTemplate("single_action_test")
        workflow.add_thread(thread, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="",
            labwares=[plate], resources_registry=registry,
            system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)

        assert status.status == "completed", (
            f"Thread should complete (release reservation, move to end). "
            f"Got: {status.status}"
        )
        await runtime.shutdown()


class TestReservationHoldoverUnit:
    """Direct unit tests for ``ReservationHoldover`` collaborator."""

    @staticmethod
    def _make_action(location: object, *, only_residents_left: bool = False):
        from unittest.mock import MagicMock
        from orca.workflow_models.actions.executable_location_action import (
            ExecutableLocationAction,
        )

        action = MagicMock(spec=ExecutableLocationAction)
        action.action = MagicMock()
        action.action.release_when_drained = MagicMock()
        action.action.location = location
        action.action.release_reservation = MagicMock()
        action.action.set_residency_check = MagicMock()
        action.action.only_residents_remain = MagicMock(return_value=only_residents_left)
        action.action.reservation = MagicMock()
        action.action.reservation.pending_drain_check = None
        return action

    def test_new_holdover_has_no_current(self) -> None:
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )

        holdover = ReservationHoldover()
        assert holdover.current() is None
        assert holdover.has_current() is False

    def test_release_current_is_idempotent_when_empty(self) -> None:
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )

        holdover = ReservationHoldover()
        holdover.release_current()
        assert holdover.current() is None

    def test_release_current_releases_and_clears(self) -> None:
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )

        holdover = ReservationHoldover()
        action = self._make_action(location="dev_a")
        holdover.acquire_after_action(action, owns_reservation=True)
        holdover.release_current()
        action.action.release_reservation.assert_called_once()
        assert holdover.current() is None

    def test_maybe_release_for_next_action_holds_when_location_matches(self) -> None:
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )

        holdover = ReservationHoldover()
        action = self._make_action(location="dev_a")
        holdover.acquire_after_action(action, owns_reservation=True)
        holdover.maybe_release_for_next_action(["dev_a", "dev_b"])
        action.action.release_reservation.assert_not_called()
        assert holdover.current() is action

    def test_maybe_release_for_next_action_releases_when_location_absent(self) -> None:
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )

        holdover = ReservationHoldover()
        action = self._make_action(location="dev_a")
        holdover.acquire_after_action(action, owns_reservation=True)
        holdover.maybe_release_for_next_action(["dev_b", "dev_c"])
        # Departure boundary: release is drain-gated; the old
        # direct release_reservation call was the ungated-release defect.
        action.action.release_when_drained.assert_called_once()
        assert holdover.current() is None

    def test_maybe_release_for_next_action_noop_when_empty(self) -> None:
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )

        holdover = ReservationHoldover()
        holdover.maybe_release_for_next_action(["dev_a"])
        assert holdover.current() is None

    def test_maybe_release_for_next_action_singleton_same_device(self) -> None:
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )

        holdover = ReservationHoldover()
        action = self._make_action(location="dev_a")
        holdover.acquire_after_action(action, owns_reservation=True)
        holdover.maybe_release_for_next_action({"dev_a"})
        action.action.release_reservation.assert_not_called()
        assert holdover.current() is action

    def test_maybe_release_for_next_action_singleton_different_device(self) -> None:
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )

        holdover = ReservationHoldover()
        action = self._make_action(location="dev_a")
        holdover.acquire_after_action(action, owns_reservation=True)
        holdover.maybe_release_for_next_action({"dev_b"})
        # Departure boundary: drain-gated release (see rule-7 note above).
        action.action.release_when_drained.assert_called_once()
        assert holdover.current() is None

    def test_acquire_after_action_from_empty_owner(self) -> None:
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )

        holdover = ReservationHoldover()
        action = self._make_action(location="dev_a")
        holdover.acquire_after_action(action, owns_reservation=True)
        action.action.release_reservation.assert_not_called()
        assert holdover.current() is action

    def test_acquire_after_action_from_empty_non_owner(self) -> None:
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )

        holdover = ReservationHoldover()
        action = self._make_action(location="dev_a")
        holdover.acquire_after_action(action, owns_reservation=False)
        action.action.release_reservation.assert_not_called()
        assert holdover.current() is None

    def test_acquire_after_action_replaces_owner_to_owner(self) -> None:
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )

        holdover = ReservationHoldover()
        prev = self._make_action(location="dev_a")
        holdover.acquire_after_action(prev, owns_reservation=True)
        next_action = self._make_action(location="dev_b")
        holdover.acquire_after_action(next_action, owns_reservation=True)
        prev.action.release_reservation.assert_called_once()
        next_action.action.release_reservation.assert_not_called()
        assert holdover.current() is next_action

    def test_acquire_after_action_replaces_owner_to_non_owner(self) -> None:
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )

        holdover = ReservationHoldover()
        prev = self._make_action(location="dev_a")
        holdover.acquire_after_action(prev, owns_reservation=True)
        next_action = self._make_action(location="dev_b")
        holdover.acquire_after_action(next_action, owns_reservation=False)
        prev.action.release_reservation.assert_called_once()
        assert holdover.current() is None

    def test_release_after_move_if_drained_noop_when_empty(self) -> None:
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )

        holdover = ReservationHoldover()
        holdover.release_after_move_if_drained()
        assert holdover.current() is None

    def test_release_after_move_if_drained_keeps_while_a_traveller_remains(self) -> None:
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )

        holdover = ReservationHoldover()
        action = self._make_action(location="dev_a", only_residents_left=False)
        holdover.acquire_after_action(action, owns_reservation=True)
        holdover.release_after_move_if_drained()
        action.action.release_reservation.assert_not_called()
        assert holdover.current() is action

    def test_release_after_move_if_drained_releases_when_drained(self) -> None:
        from unittest.mock import MagicMock
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )

        holdover = ReservationHoldover()
        action = self._make_action(location=MagicMock(), only_residents_left=True)
        action.action.location.resource = MagicMock()
        holdover.acquire_after_action(action, owns_reservation=True)
        holdover.release_after_move_if_drained()
        action.action.release_reservation.assert_called_once()
        assert holdover.current() is None


    def test_force_release_swallows_value_error(self) -> None:
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )

        holdover = ReservationHoldover()
        action = self._make_action(location="dev_a")
        action.action.release_reservation.side_effect = ValueError("already released")
        holdover.acquire_after_action(action, owns_reservation=True)
        holdover.force_release()
        assert holdover.current() is None

    def test_force_release_noop_when_empty(self) -> None:
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )

        holdover = ReservationHoldover()
        holdover.force_release()
        assert holdover.current() is None

    def test_force_release_clears_after_successful_release(self) -> None:
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )

        holdover = ReservationHoldover()
        action = self._make_action(location="dev_a")
        holdover.acquire_after_action(action, owns_reservation=True)
        holdover.force_release()
        action.action.release_reservation.assert_called_once()
        assert holdover.current() is None

    def test_force_release_reaches_every_hold_left_draining(self) -> None:
        """A thread that dies mid-departure must not strand a device. Its drain
        predicate can never pass once the thread is gone, and one thread can
        have left a hold on more than one device."""
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )
        holdover = ReservationHoldover()
        first = self._make_action(location="dev_a")
        second = self._make_action(location="dev_b")
        for action in (first, second):
            holdover.acquire_after_action(action, owns_reservation=True)
            holdover.release_current_when_drained()

        holdover.force_release()
        first.action.release_reservation.assert_called_once()
        second.action.release_reservation.assert_called_once()

    def test_a_departure_releases_a_hold_that_has_since_drained(self) -> None:
        """A device nothing is left on must not read as reserved just because
        no one has asked for it yet."""
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )
        holdover = ReservationHoldover()
        action = self._make_action(location="dev_a")
        action.action.reservation.pending_drain_check = lambda _exclude: True
        holdover.acquire_after_action(action, owns_reservation=True)
        holdover.release_current_when_drained()
        holdover.settle_drains_on_departure()

        action.action.reservation.release_reservation.assert_called_once()
        holdover.force_release()
        action.action.release_reservation.assert_not_called()

    def test_a_departure_leaves_an_undrained_hold_to_the_manager(self) -> None:
        """An occupant that is still going to leave keeps the hold open, and the
        departing thread must not drop it on its way out."""
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )

        holdover = ReservationHoldover()
        action = self._make_action(location="dev_a")
        action.action.reservation.pending_drain_check = lambda _exclude: False
        holdover.acquire_after_action(action, owns_reservation=True)
        holdover.release_current_when_drained()
        holdover.settle_drains_on_departure()

        action.action.reservation.release_reservation.assert_not_called()
        holdover.force_release()
        action.action.release_reservation.assert_not_called()

    def test_a_failing_drain_check_does_not_take_the_thread_down(self) -> None:
        """The early release is a courtesy. If it throws, the thread is past
        its last action and must still finish, the hold that could not answer
        stays with the thread for terminal cleanup, and a hold on another
        device that answered fine is not dragged down with it."""
        from orca.workflow_models.labware_threads.reservation_holdover import (
            ReservationHoldover,
        )

        def _boom(_exclude: str | None) -> bool:
            raise RuntimeError("the predicate cannot answer")

        holdover = ReservationHoldover()
        broken = self._make_action(location="dev_a")
        broken.action.reservation.pending_drain_check = _boom
        healthy = self._make_action(location="dev_b")
        healthy.action.reservation.pending_drain_check = lambda _exclude: False
        for action in (broken, healthy):
            holdover.acquire_after_action(action, owns_reservation=True)
            holdover.release_current_when_drained()

        holdover.settle_drains_on_departure()

        holdover.force_release()
        broken.action.release_reservation.assert_called_once()
        healthy.action.release_reservation.assert_not_called()
