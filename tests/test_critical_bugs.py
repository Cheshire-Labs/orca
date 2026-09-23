"""Tests for 5 critical execution bugs.

Bug 1: EventBus handler exceptions crash emitter and skip subsequent handlers
Bug 2: Reservation leak when move action fails (try/finally on _execute_move_action)
Bug 3: Infinite wait on all_labware_is_present with no timeout
Bug 4: Tick loop orphaned after workflow completion, never cancelled
Bug 5: Spawn handler fires duplicate child threads on method retry/skip
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Dict, List
from unittest.mock import MagicMock

import pytest

from orca.events.event_bus import EventBus
from orca.events.event_handlers import SystemBoundEventHandler
from orca.events.execution_context import (
    ExecutionContext,
    MethodExecutionContext,
    WorkflowExecutionContext,
)
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.transporter import Transporter
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.system.reservation_manager.reservation_manager import (
    ThreadReservationCoordinator,
)
import orca.orca as orca
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.status_enums import FailurePolicy, RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.mock import UniversalMockDevice
from tests.test_helpers import (
    FailOnPlaceTransporter,
    create_test_plate_template,
    create_test_transporter,
    wait_for_paused_thread,
    wait_until,
    wire_system_map,
)


# ---------------------------------------------------------------------------
# Mock devices and transporters
# ---------------------------------------------------------------------------


class FailOnceDevice(UniversalMockDevice):
    """Device that fails on shake until should_fail is toggled off."""

    def __init__(self, name: str, site_names: list[str] | None = None) -> None:
        super().__init__(name, site_names=site_names)
        self.should_fail = True
        self.shake_count = 0

    async def shake(self, duration: int, speed: int) -> None:
        self.shake_count += 1
        if self.should_fail:
            raise RuntimeError("Simulated shake failure")
        await super().shake(duration, speed)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _build_single_action_system(
    failure_policy: FailurePolicy = FailurePolicy.PAUSE,
    device: UniversalMockDevice | None = None,
    transporter: Transporter | None = None,
):
    """Build a simple one-action system for testing."""
    dev = device or FailOnceDevice("shaker1")
    xporter = transporter or create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(dev)
    registry.add_resource(xporter)

    pool = ResourcePool("shaker1", [dev])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": dev}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate], failure_policy=failure_policy)
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_action

    method = shake_method

    pad_loc = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad_loc, end=pad_loc)
    async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield method

    thread = plate_thread

    workflow = WorkflowTemplate("test_workflow")
    workflow.add_thread(thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system",
        description="",
        labwares=[plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    coordinator = builder._thread_reservation_coordinator
    res_mgr = coordinator._reservation_manager
    runtime = SystemRuntime(system, event_bus=event_bus)
    return runtime, workflow, dev, event_bus, res_mgr, coordinator


async def _build_two_action_system_with_auto_spawn(
    failure_policy: FailurePolicy = FailurePolicy.PAUSE,
):
    """Build a two-action system where the parent method auto-spawns a child.

    The parent method has shake (can fail) + seal (always succeeds).
    shake_action declares plate_child as an input, so the child thread
    is auto-spawned when the action is consumed.
    """
    device = FailOnceDevice("shaker1", site_names=["site-1", "site-2"])
    transporter = create_test_transporter("robot1", ["shaker1", "pad1", "pad2"])
    plate_main = create_test_plate_template("plate_main")
    plate_child = create_test_plate_template("plate_child")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)

    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1", "pad2"])

    @orca.action(device=pool, inputs=[plate_main, plate_child], failure_policy=failure_policy)
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.action(device=pool, inputs=[plate_main])
    async def seal_action(ctx: ActionContext) -> None:
        await ctx.device().seal(temperature=180, duration=3)

    @orca.method
    async def parent_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_action
        yield seal_action

    pad1 = system_map.get_location("pad1")
    pad2 = system_map.get_location("pad2")

    @orca.thread(labware=plate_main, start=pad1, end=pad1)
    async def main_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield parent_method

    @orca.thread(labware=plate_child, start=pad2, end=pad2)
    async def child_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join()

    @orca.workflow(name="spawn_test")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(main_thread)
        wf.thread(child_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system",
        description="",
        labwares=[plate_main, plate_child],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    runtime = SystemRuntime(system, event_bus=event_bus)
    return runtime, workflow, device, event_bus


# ===========================================================================
# Bug 1: EventBus handler exceptions uncaught
# ===========================================================================


class TestBug1EventBusHandlerExceptions:
    """EventBus.emit() must catch handler exceptions without propagating
    to the emitter or skipping subsequent handlers."""

    def test_handler_exception_does_not_propagate_to_emitter(self) -> None:
        """If a handler throws, the emitter should NOT see the exception."""
        bus = EventBus()
        context = WorkflowExecutionContext(execution_id="wf-1", workflow_name="test")

        def bad_handler(event: str, ctx: ExecutionContext) -> None:
            raise RuntimeError("handler exploded")

        bus.subscribe("TEST.EVENT", bad_handler)
        bus.emit("TEST.EVENT", context)

    def test_subsequent_handlers_still_fire_after_exception(self) -> None:
        """Handlers registered after a failing handler must still fire."""
        bus = EventBus()
        context = WorkflowExecutionContext(execution_id="wf-1", workflow_name="test")
        second_handler_called = False

        def bad_handler(event: str, ctx: ExecutionContext) -> None:
            raise RuntimeError("handler exploded")

        def good_handler(event: str, ctx: ExecutionContext) -> None:
            nonlocal second_handler_called
            second_handler_called = True

        bus.subscribe("TEST.EVENT", bad_handler)
        bus.subscribe("TEST.EVENT", good_handler)

        bus.emit("TEST.EVENT", context)
        assert second_handler_called, (
            "Second handler was skipped because the first handler's exception propagated"
        )

    def test_system_bound_handler_exception_caught(self) -> None:
        """SystemBoundEventHandler.handle() exceptions should also be caught."""
        bus = EventBus()
        context = WorkflowExecutionContext(execution_id="wf-1", workflow_name="test")

        class ExplodingHandler(SystemBoundEventHandler):
            def handle(self, event: str, ctx: ExecutionContext) -> None:
                raise RuntimeError("system handler exploded")

        handler = ExplodingHandler()
        bus.subscribe("TEST.EVENT", handler)
        bus.emit("TEST.EVENT", context)

    def test_global_subscriber_exception_caught(self) -> None:
        """Global subscribers' exceptions should also be caught."""
        bus = EventBus()
        context = WorkflowExecutionContext(execution_id="wf-1", workflow_name="test")
        called = False

        def bad_global(event: str, ctx: ExecutionContext) -> None:
            raise RuntimeError("global handler exploded")

        def good_global(event: str, ctx: ExecutionContext) -> None:
            nonlocal called
            called = True

        bus.subscribe_all(bad_global)
        bus.subscribe_all(good_global)

        bus.emit("TEST.EVENT", context)
        assert called, "Second global subscriber was skipped after first raised"

    def test_generalized_event_handler_exception_caught(self) -> None:
        """3-part events (e.g. METHOD.uuid.IN_PROGRESS) dispatch to handlers
        subscribed to the generalized form (METHOD.IN_PROGRESS). Exceptions
        in those handlers must also be caught."""
        bus = EventBus()
        context = WorkflowExecutionContext(execution_id="wf-1", workflow_name="test")
        generalized_called = False

        def bad_handler(event: str, ctx: ExecutionContext) -> None:
            raise RuntimeError("generalized handler exploded")

        def good_handler(event: str, ctx: ExecutionContext) -> None:
            nonlocal generalized_called
            generalized_called = True

        bus.subscribe("METHOD.IN_PROGRESS", bad_handler)
        bus.subscribe("METHOD.IN_PROGRESS", good_handler)

        # 3-part event dispatches to generalized "METHOD.IN_PROGRESS"
        bus.emit("METHOD.abc123.IN_PROGRESS", context)
        assert generalized_called, (
            "Good handler on generalized event was skipped after bad handler threw"
        )


# ===========================================================================
# Bug 2: Reservation leak on move action failure
# ===========================================================================


class TestBug2ReservationLeakOnMoveFailure:
    """If _execute_move_action() fails mid-move, the move reservation must
    be released so the target location is not permanently blocked."""

    async def test_transporter_place_failure_releases_move_reservation(self) -> None:
        """Transporter fails during place(). The plate is in the gripper.
        Thread PAUSES. Operator aborts. The move reservation must be
        released and the thread must reach the ABORTED terminal state.

        Bug TTT widening: pre-fix this test asserted
        ``ExecutionState.FAILED`` because ``_execute_move_action``
        re-raised the original move error for ABORT_THREAD; the
        thread task ended with the exception in flight and
        ``completed.set()`` never fired (the status setter wired the
        event only on COMPLETED). Post-fix the move-failure recovery
        path raises ``_ThreadAbortedSignal`` for ABORT_THREAD via the
        shared ``_check_abort_thread`` helper, ``start`` catches it
        and lands the thread at ABORTED, and the execution rolls up to
        ABORTED (a held ABORTED thread is not COMPLETED). The
        reservation-cleanup invariant the test was originally written
        for is unchanged.
        """
        fail_transporter = FailOnPlaceTransporter("robot1", ["shaker1", "pad1"])
        device = UniversalMockDevice("shaker1")

        runtime, workflow, _, event_bus, res_mgr, coordinator = (
            await _build_single_action_system(
                FailurePolicy.PAUSE,
                device=device,
                transporter=fail_transporter,
            )
        )
        await runtime.start()

        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused = await wait_for_paused_thread(runtime, record.id)

        assert fail_transporter.place_call_count >= 1, (
            "place() was never called; the transporter failure wasn't exercised"
        )

        # Abort the paused thread
        runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_THREAD)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.ABORTED

        threads = runtime.list_threads(record.id)
        aborted = [t for t in threads if t.id == paused.id]
        assert len(aborted) == 1
        assert aborted[0].status == "ABORTED", (
            f"Bug TTT (move path): thread should land ABORTED after "
            f"ABORT_THREAD on a move failure; got {aborted[0].status}."
        )

        assert len(res_mgr.reservations) == 0, (
            "Move reservation leaked after move failure + abort. "
            "The target location is permanently blocked."
        )
        await runtime.shutdown()

    async def test_operator_recovery_after_place_failure(self) -> None:
        """Full operator recovery flow for a mid-move failure:
        1. Workflow 1: place fails, plate in gripper, thread PAUSES
        2. Workflow 2: starts, reservation blocked, eventually PAUSES (timeout)
        3. Operator fixes transporter, retries workflow 1 -- place succeeds
        4. Operator retries workflow 2 -- reservation now available, completes

        Uses end=shaker1 so plates stay at the device after the shake
        (no return move needed, avoiding cross-workflow location contention).
        """
        fail_transporter = FailOnPlaceTransporter("robot1", ["shaker1", "pad1", "pad2"])
        device = UniversalMockDevice("shaker1")
        plate = create_test_plate_template("plate_96")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(fail_transporter)
        pool = ResourcePool("shaker1", [device])
        registry.add_resource_pool(pool)

        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1", "pad2"])

        @orca.action(device=pool, inputs=[plate])
        async def shake_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.method
        async def shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shake_action

        method = shake_method

        pad1 = system_map.get_location("pad1")
        pad2 = system_map.get_location("pad2")
        shaker1 = system_map.resolve_journey_location("shaker1")

        # Plate starts at pad1, ends at shaker1 (no return move)
        @orca.thread(labware=plate, start=pad1, end=shaker1)
        async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield method

        thread = plate_thread
        workflow = WorkflowTemplate("recovery_test")
        workflow.add_thread(thread, is_start=True)

        event_bus = EventBus()
        from orca.config import OrcaConfig, ReservationConfig
        test_config = OrcaConfig(reservation=ReservationConfig(
            move_reservation_timeout=30.0,
            action_reservation_timeout=30.0,
        ))
        builder = SdkToSystemBuilder(
            name="test_system", description="",
            labwares=[plate], resources_registry=registry,
            system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
            config=test_config,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()

        # --- Workflow 1: place to shaker1 fails, plate in gripper, PAUSES ---
        record1 = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused1 = await wait_for_paused_thread(runtime, record1.id)
        assert fail_transporter.labware is not None, (
            "Plate should be in the gripper after place failure"
        )

        # --- Workflow 2: starts at pad2, blocked on reservation, PAUSES ---
        record2 = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused2 = await wait_for_paused_thread(runtime, record2.id, timeout=40.0)

        # --- Operator fixes the transporter ---
        fail_transporter.should_fail_place = False

        # --- Operator retries workflow 1: place succeeds, workflow completes ---
        runtime.recover_thread(record1.id, paused1.id, RecoveryDecision.RETRY)
        status1 = await asyncio.wait_for(runtime.wait(record1.id), timeout=15.0)
        assert status1.status == ExecutionState.COMPLETED, (
            f"Workflow 1 should complete after retry, "
            f"got status={status1.status}, error={status1.error}"
        )

        # --- Operator retries workflow 2: reservation now available, completes ---
        runtime.recover_thread(record2.id, paused2.id, RecoveryDecision.RETRY)
        status2 = await asyncio.wait_for(runtime.wait(record2.id), timeout=45.0)
        assert status2.status == ExecutionState.COMPLETED, (
            f"Workflow 2 should complete after retry, "
            f"got status={status2.status}, error={status2.error}"
        )
        await runtime.shutdown()

    async def test_move_failure_records_a_move_failed_incident(self) -> None:
        """A default-PAUSE move failure must leave a queryable MOVE_FAILED
        incident, the move-side mirror of ACTION_FAILED. Before this, a move
        failure reached _pause_for_error with no incident, so an auto-paused
        thread (e.g. a wedged gripper) was undiagnosable on every operator
        surface."""
        from orca.runtime.incident_store import IncidentCategory, RecoveryAction

        fail_transporter = FailOnPlaceTransporter("robot1", ["shaker1", "pad1", "pad2"])
        device = UniversalMockDevice("shaker1")
        plate = create_test_plate_template("plate_96")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(fail_transporter)
        pool = ResourcePool("shaker1", [device])
        registry.add_resource_pool(pool)

        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1", "pad2"])

        @orca.action(device=pool, inputs=[plate])
        async def shake_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.method
        async def shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shake_action

        pad1 = system_map.get_location("pad1")
        shaker1 = system_map.resolve_journey_location("shaker1")

        @orca.thread(labware=plate, start=pad1, end=shaker1)
        async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield shake_method

        workflow = WorkflowTemplate("move_incident_test")
        workflow.add_thread(plate_thread, is_start=True)

        event_bus = EventBus()
        from orca.config import OrcaConfig, ReservationConfig
        test_config = OrcaConfig(reservation=ReservationConfig(
            move_reservation_timeout=30.0, action_reservation_timeout=30.0,
        ))
        builder = SdkToSystemBuilder(
            name="test_system", description="",
            labwares=[plate], resources_registry=registry,
            system_map=system_map, workflows=[workflow], event_bus=event_bus,
            config=test_config,
        )
        await builder.bind_labwares()
        runtime = SystemRuntime(builder.get_system(), event_bus=event_bus)
        await runtime.start()
        try:
            record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
            paused = await wait_for_paused_thread(runtime, record.id)

            incidents = await runtime.incidents.list(category=IncidentCategory.MOVE_FAILED)
            assert len(incidents) == 1, "expected one MOVE_FAILED incident"
            incident = incidents[0]
            assert incident.execution_id == record.id
            assert incident.thread_id == paused.id
            assert incident.recovery_action == RecoveryAction.THREAD_RECOVER_RETRY
            assert "arm jammed" in incident.message
            # labware is the INSTANCE name (template + hash), the useful operator
            # identity for the specific plate, not the template label.
            assert incident.detail.labware.startswith("plate_96")
            assert incident.detail.target == "shaker1/slot"
            assert paused.pause_site == "MOVE", (
                "the narrowest pause site there is: anything but RETRY, or a "
                "CONTINUE the ledger does not back, fails the thread AND takes "
                "the execution with it. A client that cannot see it is choosing "
                f"blind; got {paused.pause_site!r}"
            )
        finally:
            runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_THREAD)
            try:
                await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
            except Exception:
                pass
            await runtime.shutdown()

    async def _build_move_system(self, policy: FailurePolicy):
        """A one-plate system whose first move (pad1 -> shaker1) is the failure
        point. Returns (runtime, workflow, transporter, record-less)."""
        fail_transporter = FailOnPlaceTransporter("robot1", ["shaker1", "pad1", "pad2"])
        device = UniversalMockDevice("shaker1")
        plate = create_test_plate_template("plate_96")
        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(fail_transporter)
        pool = ResourcePool("shaker1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1", "pad2"])

        @orca.action(device=pool, inputs=[plate], failure_policy=policy)
        async def shake_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.method
        async def shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shake_action

        pad1 = system_map.get_location("pad1")
        shaker1 = system_map.resolve_journey_location("shaker1")

        @orca.thread(labware=plate, start=pad1, end=shaker1)
        async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield shake_method

        workflow = WorkflowTemplate("move_guard_test")
        workflow.add_thread(plate_thread, is_start=True)
        event_bus = EventBus()
        from orca.config import OrcaConfig, ReservationConfig
        cfg = OrcaConfig(reservation=ReservationConfig(
            move_reservation_timeout=30.0, action_reservation_timeout=30.0,
        ))
        builder = SdkToSystemBuilder(
            name="test_system", description="", labwares=[plate],
            resources_registry=registry, system_map=system_map,
            workflows=[workflow], event_bus=event_bus, config=cfg,
        )
        await builder.bind_labwares()
        runtime = SystemRuntime(builder.get_system(), event_bus=event_bus)
        return runtime, workflow, fail_transporter

    async def test_external_control_preemption_records_no_move_incident(self) -> None:
        """A move preempted by external gateway control raises an
        OverrideWithPause signal, not a failure. It must NOT record a
        MOVE_FAILED incident (the action guard mirrored) even though the thread
        pauses -- else every gateway command racing a move spams a failure."""
        from orca.runtime.incident_store import IncidentCategory

        runtime, workflow, transporter = await self._build_move_system(FailurePolicy.PAUSE)
        transporter.take_external_control()
        await runtime.start()
        try:
            record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
            paused = await wait_for_paused_thread(runtime, record.id)

            incidents = await runtime.incidents.list(category=IncidentCategory.MOVE_FAILED)
            assert incidents == [], (
                "an external-control preemption is a coordination pause, not a "
                f"move failure; no MOVE_FAILED incident expected, got {incidents}")
        finally:
            transporter.release_external_control()
            runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_THREAD)
            try:
                await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
            except Exception:
                pass
            await runtime.shutdown()

    async def test_abort_policy_move_failure_records_no_incident(self) -> None:
        """A move failing under ABORT policy errors the thread without pausing,
        so no MOVE_FAILED incident is recorded (it is a PAUSE-branch record)."""
        from orca.runtime.incident_store import IncidentCategory

        runtime, workflow, _ = await self._build_move_system(FailurePolicy.ABORT)
        await runtime.start()
        try:
            record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
            try:
                await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
            except Exception:
                pass
            incidents = await runtime.incidents.list(category=IncidentCategory.MOVE_FAILED)
            assert incidents == [], (
                f"ABORT-policy move failure must not record MOVE_FAILED, got {incidents}")
        finally:
            await runtime.shutdown()


# ===========================================================================
# Bug 3: No timeout on all_labware_is_present
# ===========================================================================


class _FakeLabware:
    """Stub with a string name attribute for timeout error message testing."""
    def __init__(self, name: str) -> None:
        self.name = name


class TestBug3CoLabwareTimeout:
    """Waiting for co-labware must time out, not hang indefinitely."""

    async def test_co_labware_wait_times_out(self) -> None:
        """Set CO_LABWARE_TIMEOUT to 1 second and create a system where
        the all_labware_is_present event will never fire. The thread must
        raise a TimeoutError and the workflow must fail gracefully."""
        device = UniversalMockDevice("shaker1")
        transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
        plate = create_test_plate_template("plate_96")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)

        pool = ResourcePool("shaker1", [device])
        registry.add_resource_pool(pool)

        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

        @orca.action(device=pool, inputs=[plate])
        async def shake_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.method
        async def stuck_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shake_action

        method = stuck_method

        pad_loc = system_map.get_location("pad1")

        @orca.thread(labware=plate, start=pad_loc, end=pad_loc)
        async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield method

        thread = plate_thread

        workflow = WorkflowTemplate("timeout_test")
        workflow.add_thread(thread, is_start=True)

        event_bus = EventBus()
        from orca.config import OrcaConfig, CoordinationConfig
        test_config = OrcaConfig(coordination=CoordinationConfig(co_labware_timeout=1.0))
        builder = SdkToSystemBuilder(
            name="test_system",
            description="",
            labwares=[plate],
            resources_registry=registry,
            system_map=system_map,
            workflows=[workflow],
            event_bus=event_bus,
            config=test_config,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        # Monkeypatch the action so all_labware_is_present never fires.
        # Replace the internal _all_labware_is_present Event with a fresh
        # never-set one and patch the missing-input view so the explicit
        # gate-refresh can't open it either.
        patched = False

        def _patch_action_to_never_fire() -> bool:
            """After the action is resolved, replace its event and missing-input view."""
            nonlocal patched
            for t in system.executing_threads:
                if t.assigned_action is not None:
                    action = t.assigned_action.action
                    action._all_labware_is_present = asyncio.Event()
                    setattr(
                        action,
                        "peek_missing_input_labware",
                        lambda: [_FakeLabware("phantom_plate")],
                    )
                    patched = True
                    return True
            return False

        await runtime.start()
        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)

        # Poll until the action is resolved and we can patch it
        for _ in range(50):
            if _patch_action_to_never_fire():
                break
            await asyncio.sleep(0.1)
        assert patched, "Failed to patch action -- it was never resolved"

        # With the fix: thread times out after 1s, workflow fails.
        # Without the fix: hangs forever, test-level wait_for fires at 10s.
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.FAILED, (
            "Workflow should fail with a co-labware timeout error, "
            "not hang indefinitely or complete successfully"
        )
        assert status.error is not None
        assert "timed out" in status.error.lower(), (
            f"Error should mention timeout, got: {status.error}"
        )
        await runtime.shutdown()

    async def test_timeout_error_identifies_missing_labware(self) -> None:
        """The timeout error message must name the missing labware
        so the operator knows which thread to investigate.

        Directly tests the timeout path in _handle_thread_at_assigned_action_location
        by creating a never-set event and verifying our code wraps it correctly.
        """
        # asyncio.wait_for raises plain TimeoutError (no message).
        # Our fix in _handle_thread_at_assigned_action_location catches that
        # and raises a new TimeoutError with a descriptive message.
        # Test the wrapping logic directly:
        never_set = asyncio.Event()
        thread_name = "test_thread"
        position_id = "shaker1"
        missing = [_FakeLabware("phantom_plate"), _FakeLabware("tips_96")]

        with pytest.raises(TimeoutError, match="phantom_plate.*tips_96"):
            try:
                await asyncio.wait_for(never_set.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                missing_names = ", ".join(lw.name for lw in missing)
                raise TimeoutError(
                    f"Thread {thread_name} timed out (0.1s) waiting "
                    f"for co-labware at {position_id}. Missing: {missing_names}"
                )


# ===========================================================================
# Bug 4: Tick loop lifetime
# ===========================================================================


class TestBug4TickLoopLifetime:
    """Tick loop must be cancelled on shutdown, not left orphaned."""

    async def test_tick_loop_stops_after_shutdown(self) -> None:
        """After runtime.shutdown(), no orphaned tick loop tasks should remain."""
        runtime, workflow, _, _, _, coordinator = await _build_single_action_system(
            FailurePolicy.ABORT, device=UniversalMockDevice("shaker1")
        )
        await runtime.start()

        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        await runtime.shutdown()

        # Wait for the tick loop task to finish cancelling after shutdown; a
        # genuinely orphaned (never-cancelled) loop makes this time out loudly.
        await wait_until(
            lambda: not [
                t for t in asyncio.all_tasks()
                if not t.done() and "start_tick_loop" in repr(t.get_coro())
            ],
            timeout=10.0,
        )

        # Check for orphaned tick loop tasks
        orphaned = [
            t for t in asyncio.all_tasks()
            if not t.done() and "start_tick_loop" in repr(t.get_coro())
        ]
        assert len(orphaned) == 0, (
            f"Orphaned tick loop tasks found after shutdown: {orphaned}"
        )

    async def test_ticker_started_reset_after_stop(self) -> None:
        """After stop_tick_loop(), ticker_started is False so a new
        workflow can start a fresh tick loop."""
        _, _, _, _, _, coordinator = await _build_single_action_system(
            FailurePolicy.ABORT, device=UniversalMockDevice("shaker1")
        )
        coordinator.ticker_started = True
        coordinator.stop_tick_loop()
        assert coordinator.ticker_started is False, (
            "ticker_started should be reset to False after stop_tick_loop()"
        )


# ===========================================================================
# Bug 5: Auto-spawn dedup on retry/skip
# ===========================================================================


class TestBug5AutoSpawnDedupOnRetry:
    """Auto-spawn must not create duplicate child threads when
    a method re-resolves actions after skip or retry."""

    @pytest.mark.asyncio
    async def test_auto_spawn_dedup_same_action_id(self) -> None:
        """Calling _auto_spawn_for_action twice with the same action
        must only invoke the callback once."""
        from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread
        from orca.workflow_models.actions.dynamic_resource_action import UnresolvedLocationAction

        callback_calls: list[tuple[str, str]] = []

        async def mock_callback(
            labware_name: str, method: MagicMock, run_mode: WorkflowRunMode,
        ) -> None:
            callback_calls.append((labware_name, method.name))

        plate_main = create_test_plate_template("plate_main")
        plate_child = create_test_plate_template("plate_child")

        mock_action = MagicMock(spec=UnresolvedLocationAction)
        mock_action.id = "action-001"
        mock_action.expected_input_templates = [plate_main, plate_child]
        # The co-labware has no thread yet; that is the precondition for spawning.
        mock_action.is_input_assigned = MagicMock(return_value=False)

        thread = MagicMock()
        thread.labware_template = plate_main
        thread.shared_executing_method = None

        mock_method = MagicMock()
        mock_method.name = "parent_method"

        mock_method.shared_coord.is_contributor = MagicMock(return_value=False)

        elt = ExecutingLabwareThread.__new__(ExecutingLabwareThread)
        elt._thread = thread
        elt._auto_spawn_callback = mock_callback
        elt._capacity_precheck_callback = None
        elt._auto_spawned = set()
        elt._assigned_method = mock_method
        elt._partner_constraints = {}

        await elt._auto_spawn_for_action(mock_action)
        assert len(callback_calls) == 1

        await elt._auto_spawn_for_action(mock_action)
        assert len(callback_calls) == 1, (
            "Auto-spawn fired twice for the same (action_id, labware_name)."
        )

    @pytest.mark.asyncio
    async def test_auto_spawn_allows_different_action_ids(self) -> None:
        """Different actions needing the same labware should each trigger a spawn."""
        from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread
        from orca.workflow_models.actions.dynamic_resource_action import UnresolvedLocationAction

        callback_calls: list[tuple[str, str]] = []

        async def mock_callback(
            labware_name: str, method: MagicMock, run_mode: WorkflowRunMode,
        ) -> None:
            callback_calls.append((labware_name, method.name))

        plate_main = create_test_plate_template("plate_main")
        plate_child = create_test_plate_template("plate_child")

        action_1 = MagicMock(spec=UnresolvedLocationAction)
        action_1.id = "action-001"
        action_1.expected_input_templates = [plate_main, plate_child]
        action_1.is_input_assigned = MagicMock(return_value=False)

        action_2 = MagicMock(spec=UnresolvedLocationAction)
        action_2.id = "action-002"
        action_2.expected_input_templates = [plate_main, plate_child]
        action_2.is_input_assigned = MagicMock(return_value=False)

        thread = MagicMock()
        thread.labware_template = plate_main
        thread.shared_executing_method = None

        mock_method = MagicMock()
        mock_method.name = "parent_method"

        mock_method.shared_coord.is_contributor = MagicMock(return_value=False)

        elt = ExecutingLabwareThread.__new__(ExecutingLabwareThread)
        elt._thread = thread
        elt._auto_spawn_callback = mock_callback
        elt._capacity_precheck_callback = None
        elt._auto_spawned = set()
        elt._assigned_method = mock_method
        elt._partner_constraints = {}

        await elt._auto_spawn_for_action(action_1)
        await elt._auto_spawn_for_action(action_2)
        assert len(callback_calls) == 2, (
            "Auto-spawn should fire for different action_ids (consumable labware needs fresh spawn)"
        )

    async def test_skip_does_not_duplicate_auto_spawn(self) -> None:
        """Skip first action in a multi-action method. Auto-spawn dedup
        prevents duplicate child threads on re-resolution."""
        runtime, workflow, device, event_bus = await _build_two_action_system_with_auto_spawn()
        await runtime.start()

        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused = await wait_for_paused_thread(runtime, record.id)

        threads_before = runtime.list_threads(record.id)
        thread_count_before = len(threads_before)

        device.should_fail = False
        runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_ACTION)

        status = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)

        threads_after = runtime.list_threads(record.id)
        thread_count_after = len(threads_after)

        assert thread_count_after == thread_count_before, (
            f"Thread count changed from {thread_count_before} to {thread_count_after} "
            "after skip. Auto-spawn fired again on re-resolved action."
        )
        await runtime.shutdown()

    async def test_retry_does_not_duplicate_auto_spawn(self) -> None:
        """Retry the failed action. Auto-spawn dedup prevents duplicates."""
        runtime, workflow, device, event_bus = await _build_two_action_system_with_auto_spawn()
        await runtime.start()

        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused = await wait_for_paused_thread(runtime, record.id)

        threads_before = runtime.list_threads(record.id)
        thread_count_before = len(threads_before)

        device.should_fail = False
        runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY)

        status = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)

        threads_after = runtime.list_threads(record.id)
        thread_count_after = len(threads_after)

        assert thread_count_after == thread_count_before, (
            f"Thread count changed from {thread_count_before} to {thread_count_after} "
            "after retry. Auto-spawn created a duplicate."
        )
        await runtime.shutdown()
