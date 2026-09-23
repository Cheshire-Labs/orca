"""Tests for error recovery: pause, resume, retry, skip, abort.

Validates that when an action fails, the thread pauses (FailurePolicy.PAUSE)
or propagates (FailurePolicy.ABORT), and that recovery decisions work correctly.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field

import pytest

from orca.events.execution_context import WorkflowExecutionContext
from orca.resource_models.resource_pool import ResourcePool
from orca.state.records import (
    ActionContinuedDetails,
    DeclaredTracking,
    DeviceOperation,
    TrackingSource,
)
from orca.runtime.incident_store import (
    IncidentCategory,
    IncidentSeverity,
    RecoveryAction,
)
from orca.runtime.db.engine import create_memory_engine
from orca.runtime.execution_record_service import ExecutionRecordService
from orca.runtime.execution_tracking_sink import ExecutionTrackingSink
from orca.runtime.sinks import AlertSink, CollectorSink
from orca.runtime.sqlite_execution_record_store import SqliteExecutionRecordStore
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.events import EventBus
from orca.system.reservation_manager.errors import (
    ActionContinuedContext,
    MoveContinuedContext,
)
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.system.reservation_manager.reservation_manager import LocationReservationManager
import orca.orca as orca
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.labware_threads.executing_labware_thread import _recovery_event_for
from orca.workflow_models.labware_threads.thread_state_machine import ThreadEvent
from orca.workflow_models.method import ExecutingMethod, MethodInstance
from orca.workflow_models.status_enums import FailurePolicy, RecoveryDecision
from orca.workflow_models.status_manager import StatusManager
from orca.runtime.run_modes import WorkflowRunMode
from tests.mock import UniversalMockDevice
from tests.test_helpers import (
    FailOnPlaceTransporter,
    create_test_plate_template,
    create_test_transporter,
    wait_for_paused_thread,
    wait_for_paused_threads,
    wait_for_runtime_condition,
    wire_system_map,
)


@dataclass
class MockShakeCall:
    duration: int
    speed: int
    succeeded: bool


class FailingDevice(UniversalMockDevice):
    """Device whose shake() fails until told to succeed.

    Toggle `should_fail` to control behavior. Every call is recorded
    in `shake_calls` so tests can assert on call count and outcomes.
    """

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.should_fail = True
        self.shake_calls: list[MockShakeCall] = []
        # Override to raise a non-RuntimeError (e.g. an
        # OverrideWithPauseError) so tests can exercise the
        # external-coordination-pause path distinctly from an action-body
        # failure.
        self.error_factory: Callable[[], Exception] = lambda: RuntimeError(
            "Simulated shake failure"
        )

    async def shake(self, duration: int, speed: int) -> None:
        if self.should_fail:
            self.shake_calls.append(MockShakeCall(duration, speed, succeeded=False))
            raise self.error_factory()
        await super().shake(duration, speed)
        self.shake_calls.append(MockShakeCall(duration, speed, succeeded=True))


@dataclass
class FailingSystemFixture:
    """All objects needed to test error recovery scenarios."""
    runtime: SystemRuntime
    workflow: WorkflowTemplate
    device: FailingDevice
    event_bus: EventBus
    res_mgr: LocationReservationManager
    # Names of the second-action bodies that ran, so a test can prove the
    # thread reached the NEXT action rather than merely completing.
    later_action_calls: list[str] = field(default_factory=list)
    # Set only by the move-failing builder, whose mover records the places it
    # was asked to make.
    transporter: FailOnPlaceTransporter | None = None


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

async def _build_failing_system(
    failure_policy: FailurePolicy = FailurePolicy.PAUSE,
) -> FailingSystemFixture:
    """Build a single-action system with a FailingDevice."""
    device = FailingDevice("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)

    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate], failure_policy=failure_policy)
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    async def _shake_gen(ctx: object) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_action

    method = MethodTemplate("shake_method", func=_shake_gen)

    pad_loc = system_map.get_location("pad1")

    async def _thread_gen(ctx: object) -> AsyncGenerator[MethodTemplate, None]:
        yield method

    thread = ThreadTemplate(
        labware_template=plate,
        start=pad_loc,
        end=pad_loc,
        func=_thread_gen,
    )

    workflow = WorkflowTemplate("failing_workflow")
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
    res_mgr = builder._thread_reservation_coordinator._reservation_manager
    runtime = SystemRuntime(system, event_bus=event_bus)
    return FailingSystemFixture(runtime, workflow, device, event_bus, res_mgr)


async def _build_two_action_failing_system(
    failure_policy: FailurePolicy = FailurePolicy.PAUSE,
) -> FailingSystemFixture:
    """Build system with shake (can fail) + seal (always succeeds) on one device."""
    device = FailingDevice("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)

    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate], failure_policy=failure_policy)
    async def shake(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    later_action_calls: list[str] = []

    @orca.action(device=pool, inputs=[plate])
    async def seal(ctx: ActionContext) -> None:
        await ctx.device().seal(temperature=180, duration=3)
        later_action_calls.append("seal")

    shake_action = shake
    seal_action = seal

    async def _two_action_gen(ctx: object) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_action
        yield seal_action

    method = MethodTemplate("two_action_method", func=_two_action_gen)

    pad_loc = system_map.get_location("pad1")

    async def _thread_gen(ctx: object) -> AsyncGenerator[MethodTemplate, None]:
        yield method

    thread = ThreadTemplate(
        labware_template=plate,
        start=pad_loc,
        end=pad_loc,
        func=_thread_gen,
    )

    workflow = WorkflowTemplate("two_action_workflow")
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
    res_mgr = builder._thread_reservation_coordinator._reservation_manager
    runtime = SystemRuntime(system, event_bus=event_bus)
    return FailingSystemFixture(
        runtime, workflow, device, event_bus, res_mgr, later_action_calls,
    )


async def _build_partial_action_failing_system() -> FailingSystemFixture:
    """One action that seals (succeeds) then shakes (fails), plus a second action.

    The seal is a real device call that returned: it belongs in the ledger even
    though the action it sat in never finished.
    """
    device = FailingDevice("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

    later_action_calls: list[str] = []

    @orca.action(
        device=pool, inputs=[plate],
        declares=DeclaredTracking(wells_used={"plate_96": ["A1"]}),
    )
    async def seal_then_shake(ctx: ActionContext) -> None:
        await ctx.device().seal(temperature=180, duration=3)
        await ctx.device().shake(duration=1, speed=500)

    @orca.action(device=pool, inputs=[plate])
    async def delid(ctx: ActionContext) -> None:
        await ctx.device().delid()
        later_action_calls.append("delid")

    async def _gen(ctx: object) -> AsyncGenerator[ActionTemplate, None]:
        yield seal_then_shake
        yield delid

    method = MethodTemplate("partial_action_method", func=_gen)
    pad_loc = system_map.get_location("pad1")

    async def _thread_gen(ctx: object) -> AsyncGenerator[MethodTemplate, None]:
        yield method

    thread = ThreadTemplate(
        labware_template=plate, start=pad_loc, end=pad_loc, func=_thread_gen,
    )
    workflow = WorkflowTemplate("partial_action_workflow")
    workflow.add_thread(thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[plate],
        resources_registry=registry, system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    res_mgr = builder._thread_reservation_coordinator._reservation_manager
    runtime = SystemRuntime(system, event_bus=event_bus)
    return FailingSystemFixture(
        runtime, workflow, device, event_bus, res_mgr, later_action_calls,
    )


async def _build_move_failing_system() -> FailingSystemFixture:
    """A thread whose MOVE fails, so the pause is a move pause, not an action pause."""
    device = FailingDevice("shaker1")
    device.should_fail = False
    transporter = FailOnPlaceTransporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate])
    async def shake(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    async def _gen(ctx: object) -> AsyncGenerator[ActionTemplate, None]:
        yield shake

    method = MethodTemplate("move_method", func=_gen)
    pad_loc = system_map.get_location("pad1")

    async def _thread_gen(ctx: object) -> AsyncGenerator[MethodTemplate, None]:
        yield method

    thread = ThreadTemplate(
        labware_template=plate, start=pad_loc, end=pad_loc, func=_thread_gen,
    )
    workflow = WorkflowTemplate("move_failing_workflow")
    workflow.add_thread(thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[plate],
        resources_registry=registry, system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    res_mgr = builder._thread_reservation_coordinator._reservation_manager
    runtime = SystemRuntime(system, event_bus=event_bus)
    return FailingSystemFixture(
        runtime, workflow, device, event_bus, res_mgr, transporter=transporter,
    )


async def _build_body_failing_system() -> FailingSystemFixture:
    """An action whose Python body raises BEFORE any device call, plus a second action.

    Every other CONTINUE fixture fails inside a device call, which routes through
    the operation-recovery seam. This one reaches the plain action-error branch,
    where the pause flag is raised and the incident recorded.
    """
    device = FailingDevice("shaker1")
    device.should_fail = False
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

    later_action_calls: list[str] = []

    @orca.action(device=pool, inputs=[plate])
    async def bad_recipe(ctx: ActionContext) -> None:
        raise ValueError("recipe names a reagent the deck does not carry")

    @orca.action(device=pool, inputs=[plate])
    async def seal(ctx: ActionContext) -> None:
        await ctx.device().seal(temperature=180, duration=3)
        later_action_calls.append("seal")

    async def _gen(ctx: object) -> AsyncGenerator[ActionTemplate, None]:
        yield bad_recipe
        yield seal

    method = MethodTemplate("body_failing_method", func=_gen)
    pad_loc = system_map.get_location("pad1")

    async def _thread_gen(ctx: object) -> AsyncGenerator[MethodTemplate, None]:
        yield method

    thread = ThreadTemplate(
        labware_template=plate, start=pad_loc, end=pad_loc, func=_thread_gen,
    )
    workflow = WorkflowTemplate("body_failing_workflow")
    workflow.add_thread(thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[plate],
        resources_registry=registry, system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    res_mgr = builder._thread_reservation_coordinator._reservation_manager
    runtime = SystemRuntime(system, event_bus=event_bus)
    return FailingSystemFixture(
        runtime, workflow, device, event_bus, res_mgr, later_action_calls,
    )


# ---------------------------------------------------------------------------
# Existing tests (baseline behavior, all passing)
# ---------------------------------------------------------------------------

class TestPauseOnFailure:
    """Thread pauses when action fails with PAUSE policy."""

    async def test_pause_on_action_failure(self) -> None:
        f = await _build_failing_system(FailurePolicy.PAUSE)
        collector = CollectorSink()
        f.runtime.register_sink(collector)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused_thread = await wait_for_paused_thread(f.runtime, record.id)

        assert paused_thread.status == "PAUSED"
        assert len(f.device.shake_calls) == 1
        assert f.device.shake_calls[0].succeeded is False

        # A device-op failure surfaces as DEVICE_OP.<command>.FAILED; the action
        # itself does not ERROR (it stays suspended while the op is recovered).
        device_op_failed_events = [
            e for e in collector.events
            if e.entity_type == "DEVICE_OP" and e.status == "FAILED"
        ]
        assert len(device_op_failed_events) >= 1
        assert device_op_failed_events[0].entity_id == "shake"

        f.runtime.recover_thread(record.id, paused_thread.id, RecoveryDecision.ABORT_THREAD)
        try:
            await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        except Exception:
            pass
        await f.runtime.shutdown()

    async def test_abort_policy_propagates(self) -> None:
        """ABORT policy re-raises the exception; execution fails immediately."""
        f = await _build_failing_system(FailurePolicy.ABORT)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.FAILED
        assert len(f.device.shake_calls) == 1
        assert f.device.shake_calls[0].succeeded is False
        await f.runtime.shutdown()


class TestRecoveryDecisions:
    """Recovery decisions (retry, skip, skip_method, abort) work correctly."""

    async def test_retry_reexecutes_action(self) -> None:
        """After retry, the same action is re-resolved and executed again."""
        f = await _build_failing_system(FailurePolicy.PAUSE)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused_thread = await wait_for_paused_thread(f.runtime, record.id)

        f.device.should_fail = False
        f.runtime.recover_thread(record.id, paused_thread.id, RecoveryDecision.RETRY)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.COMPLETED
        assert len(f.device.shake_calls) == 2
        assert f.device.shake_calls[0].succeeded is False
        assert f.device.shake_calls[1].succeeded is True
        await f.runtime.shutdown()

    async def test_skip_advances_past_failed_action(self) -> None:
        """After skip, the failed action is discarded and the method continues."""
        f = await _build_failing_system(FailurePolicy.PAUSE)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused_thread = await wait_for_paused_thread(f.runtime, record.id)

        f.runtime.recover_thread(record.id, paused_thread.id, RecoveryDecision.ABORT_ACTION)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.COMPLETED
        assert len(f.device.shake_calls) == 1
        await f.runtime.shutdown()

    async def test_skip_method_completes_method(self) -> None:
        """After skip_method, all remaining actions in the method are abandoned."""
        f = await _build_failing_system(FailurePolicy.PAUSE)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused_thread = await wait_for_paused_thread(f.runtime, record.id)

        f.runtime.recover_thread(record.id, paused_thread.id, RecoveryDecision.ABORT_METHOD)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.COMPLETED
        assert len(f.device.shake_calls) == 1
        await f.runtime.shutdown()

    async def test_abort_thread_from_pause_lands_thread_aborted(self) -> None:
        """Bug TTT regression: ABORT_THREAD on an error-paused thread
        transitions the thread to the ``ABORTED`` terminal state and the
        execution lands ``ABORTED`` -- a held ABORTED thread rolls the
        execution up to ABORTED, not COMPLETED -- with no hung
        ``completed.wait()`` and no propagated exception that would land
        the execution at FAILED.

        Pre-fix this test asserted ``ExecutionState.FAILED`` because
        ``_handle_action_error`` re-raised the original action error
        for ABORT_THREAD; the asyncio task ended with the exception,
        the thread's ``completed`` event never fired (it was wired
        only to the COMPLETED status transition), and
        ``_on_task_done`` mapped the held exception to phase=FAILED.
        Post-fix the recovery path raises a private
        ``_ThreadAbortedSignal`` sentinel that ``start`` catches,
        sets status=ABORTED (which now fires the completed event),
        and returns cleanly. The action ran exactly once -- its
        failure was the trigger for the operator decision; the
        operator chose abort over retry, so no second invocation.
        """
        f = await _build_failing_system(FailurePolicy.PAUSE)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused_thread = await wait_for_paused_thread(f.runtime, record.id)

        f.runtime.recover_thread(record.id, paused_thread.id, RecoveryDecision.ABORT_THREAD)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.ABORTED
        threads = f.runtime.list_threads(record.id)
        aborted = [t for t in threads if t.id == paused_thread.id]
        assert len(aborted) == 1
        assert aborted[0].status == "ABORTED", (
            f"Bug TTT: thread should be in the ABORTED terminal state after "
            f"ABORT_THREAD recovery; got {aborted[0].status}. Pre-fix the "
            f"thread was stuck in the transient RESOLVING_ACTION_LOCATION "
            f"state set just before handle_recovery."
        )
        assert len(f.device.shake_calls) == 1
        await f.runtime.shutdown()

    async def test_abort_thread_preserves_last_error(self) -> None:
        """``thread.last_error`` survives an ABORT_THREAD decision so
        operator surfaces (snapshot, incidents, error envelope) can
        attribute the abort to its cause."""
        f = await _build_failing_system(FailurePolicy.PAUSE)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused_thread = await wait_for_paused_thread(f.runtime, record.id)

        f.runtime.recover_thread(record.id, paused_thread.id, RecoveryDecision.ABORT_THREAD)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.ABORTED

        aborted = [
            t for t in f.runtime.list_threads(record.id)
            if t.id == paused_thread.id
        ]
        assert len(aborted) == 1
        assert aborted[0].status == "ABORTED"
        assert aborted[0].last_error is not None, (
            "Finding 11: thread.last_error should survive ABORT_THREAD so "
            "operator surfaces can attribute the abort to its cause."
        )
        await f.runtime.shutdown()

    async def test_aborted_thread_record_keeps_last_error(self) -> None:
        """The record sink upserts on every THREAD transition, so the terminal
        ABORTED event must itself carry the error: driven through the real
        emitter (pause-on-error, then ABORT_THREAD), the persisted record must
        still answer WHY after the terminal transition lands."""
        f = await _build_failing_system(FailurePolicy.PAUSE)
        service = ExecutionRecordService(
            SqliteExecutionRecordStore(create_memory_engine())
        )
        f.runtime.register_sink(ExecutionTrackingSink(service))
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused_thread = await wait_for_paused_thread(f.runtime, record.id)
        f.runtime.recover_thread(record.id, paused_thread.id, RecoveryDecision.ABORT_THREAD)
        await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        detail = await service.get_detail(record.id)
        assert detail is not None
        persisted = [t for t in detail.threads if t.id == paused_thread.id]
        assert len(persisted) == 1
        assert persisted[0].status == "ABORTED"
        assert persisted[0].last_error is not None
        await f.runtime.shutdown()

    async def test_abort_method_clears_last_error_on_continue(self) -> None:
        """A thread that recovers via ABORT_METHOD and runs to COMPLETED
        must NOT carry the stale error from the aborted method. Unlike
        ABORT_THREAD (terminal, error preserved as the abort cause),
        ABORT_METHOD is a continue-recovery: the thread skips the rest of
        the method and keeps running, so a clean completion must report
        ``last_error is None`` rather than leaking the recovered error
        onto the snapshot."""
        f = await _build_failing_system(FailurePolicy.PAUSE)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused_thread = await wait_for_paused_thread(f.runtime, record.id)

        f.runtime.recover_thread(record.id, paused_thread.id, RecoveryDecision.ABORT_METHOD)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED

        completed = [
            t for t in f.runtime.list_threads(record.id)
            if t.id == paused_thread.id
        ]
        assert len(completed) == 1
        assert completed[0].status == "COMPLETED"
        assert completed[0].last_error is None, (
            "A thread that recovered via ABORT_METHOD and completed "
            "cleanly must not retain the stale error from the aborted method."
        )
        await f.runtime.shutdown()

    async def test_abort_action_clears_last_error_on_continue(self) -> None:
        """ABORT_ACTION is also continue-recovery: a clean completion
        afterward reports ``last_error is None``."""
        f = await _build_failing_system(FailurePolicy.PAUSE)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused_thread = await wait_for_paused_thread(f.runtime, record.id)

        f.runtime.recover_thread(record.id, paused_thread.id, RecoveryDecision.ABORT_ACTION)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED

        completed = [
            t for t in f.runtime.list_threads(record.id)
            if t.id == paused_thread.id
        ]
        assert len(completed) == 1
        assert completed[0].last_error is None, (
            "A thread that recovered via ABORT_ACTION and completed "
            "cleanly must not retain the stale error."
        )
        await f.runtime.shutdown()


class TestAlertSink:
    """AlertSink fires on PAUSED events."""

    async def test_alert_sink_fires_on_paused(self, caplog: pytest.LogCaptureFixture) -> None:
        f = await _build_failing_system(FailurePolicy.PAUSE)
        f.runtime.register_sink(AlertSink())
        await f.runtime.start()

        with caplog.at_level(logging.ERROR, logger="orca.alerts"):
            record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
            paused_thread = await wait_for_paused_thread(f.runtime, record.id)

        alerts = [r for r in caplog.records if r.name == "orca.alerts"]
        assert alerts, "AlertSink emitted no orca.alerts record on a PAUSED event"
        thread_alerts = [r for r in alerts if r.getMessage() == (
            f"ALERT: THREAD '{paused_thread.id}' is PAUSED (execution {record.id})"
        )]
        assert thread_alerts, (
            "AlertSink must fire an ERROR alert naming the paused thread and "
            f"its execution; got {[r.getMessage() for r in alerts]}"
        )
        assert thread_alerts[0].levelno == logging.ERROR

        f.runtime.recover_thread(record.id, paused_thread.id, RecoveryDecision.ABORT_THREAD)
        try:
            await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        except Exception:
            pass
        await f.runtime.shutdown()


class TestResumeValidation:
    """Validate that resume_with_decision rejects non-paused threads."""

    async def test_resume_non_paused_raises(self) -> None:
        """Calling recover_thread on a non-PAUSED thread raises ValueError."""
        f = await _build_failing_system(FailurePolicy.ABORT)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        threads = f.runtime.list_threads(record.id)
        with pytest.raises(ValueError, match="not paused"):
            f.runtime.recover_thread(record.id, threads[0].id, RecoveryDecision.RETRY)

        await f.runtime.shutdown()


# ---------------------------------------------------------------------------
# TDD tests: assert CORRECT behavior, expected to FAIL with current code
# ---------------------------------------------------------------------------

class TestBug1ReservationHeldDuringPause:
    """BUG-1: Reservation must be held while thread is paused.

    Current code (line 290) calls release_reservation() before pausing.
    Since Device.labware returns staged_labware (None after load), there
    is no safety net: the reservation is the ONLY barrier preventing
    another thread from claiming a location where a plate is loaded.
    """

    async def test_reservation_held_during_pause(self) -> None:
        """While paused, the location's reservation should still exist."""
        f = await _build_failing_system()
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused = await wait_for_paused_thread(f.runtime, record.id)

        assert "shaker1" in f.res_mgr.reservations, (
            "Reservation for shaker1 should be held while thread is paused. "
            "BUG-1: release_reservation() is called before pausing."
        )

        f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_THREAD)
        try:
            await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        except Exception:
            pass
        await f.runtime.shutdown()

    async def test_reservation_held_during_retry_execution(self) -> None:
        """After RETRY, the reservation should remain held while the action
        re-executes. Option B keeps the reservation continuously; it should
        never be released and reacquired."""
        f = await _build_failing_system()
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused = await wait_for_paused_thread(f.runtime, record.id)

        assert "shaker1" in f.res_mgr.reservations

        f.device.should_fail = False
        f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY)

        # Wait until the retry has left PAUSED and is re-executing the action.
        await wait_for_runtime_condition(
            f.runtime,
            lambda: not f.runtime.get_paused_threads(record.id),
            timeout=10.0,
        )
        assert "shaker1" in f.res_mgr.reservations, (
            "Reservation should remain held during retry, not released and reacquired"
        )

        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED
        await f.runtime.shutdown()


class TestConcern5AbortReleasesReservation:
    """CONCERN-5: ABORT policy must release reservation before propagating.

    Current code (lines 285-287) sets _assigned_action = None and re-raises
    without calling release_reservation(). This leaks the reservation,
    blocking subsequent workflows from using the same location.
    """

    async def test_abort_policy_releases_reservation(self) -> None:
        """After FailurePolicy.ABORT fails execution, the reservation should
        be cleaned up so subsequent workflows can use the same location."""
        f = await _build_failing_system(FailurePolicy.ABORT)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.FAILED

        assert "shaker1" not in f.res_mgr.reservations, (
            "Reservation for shaker1 should be released after ABORT policy. "
            "CONCERN-5: ABORT path doesn't call release_reservation()."
        )
        await f.runtime.shutdown()

    async def test_second_workflow_succeeds_after_abort(self) -> None:
        """A second workflow on the same system should succeed after
        the first was aborted. Verifies no leaked reservation blocks it."""
        f = await _build_failing_system(FailurePolicy.ABORT)
        await f.runtime.start()

        record1 = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        status1 = await asyncio.wait_for(f.runtime.wait(record1.id), timeout=10.0)
        assert status1.status == ExecutionState.FAILED

        # The claim under test: the abort leaked NO reservation. Checked
        # before clearing.
        assert "shaker1" not in f.res_mgr.reservations

        # Run 1's plate physically remains on the shaker's only site
        # (persist-until-clear + single occupancy); clear it so run 2 can
        # use the slot, exactly as an operator would.
        await f.runtime.labware.clear_all_labware()

        f.device.should_fail = False
        record2 = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        status2 = await asyncio.wait_for(f.runtime.wait(record2.id), timeout=10.0)

        assert status2.status == ExecutionState.COMPLETED, (
            "Second workflow should complete, but leaked reservation from "
            "first ABORT blocks reservation of shaker1."
        )
        await f.runtime.shutdown()

    async def test_recovery_abort_releases_reservation(self) -> None:
        """RecoveryDecision.ABORT_THREAD (from pause) must also release the reservation.
        This is a different code path than FailurePolicy.ABORT."""
        f = await _build_failing_system(FailurePolicy.PAUSE)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused = await wait_for_paused_thread(f.runtime, record.id)

        assert "shaker1" in f.res_mgr.reservations

        f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_THREAD)
        try:
            await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        except Exception:
            pass

        assert "shaker1" not in f.res_mgr.reservations, (
            "Reservation should be released after RecoveryDecision.ABORT_THREAD. "
            "This is a different path than FailurePolicy.ABORT."
        )
        await f.runtime.shutdown()


class TestSkipReleasesReservation:
    """SKIP and SKIP_METHOD must release the reservation after recovery.

    After BUG-1 fix retains reservation during pause, the SKIP/SKIP_METHOD
    paths must explicitly release it since the action is being discarded.
    """

    async def test_skip_releases_reservation(self) -> None:
        """After SKIP, the reservation for the skipped action's location
        should be released so other threads or subsequent actions can use it."""
        f = await _build_failing_system(FailurePolicy.PAUSE)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused = await wait_for_paused_thread(f.runtime, record.id)

        assert "shaker1" in f.res_mgr.reservations

        f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_ACTION)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED

        assert "shaker1" not in f.res_mgr.reservations, (
            "Reservation should be released after SKIP"
        )
        await f.runtime.shutdown()

    async def test_skip_method_releases_reservation(self) -> None:
        """After SKIP_METHOD, the reservation should be released."""
        f = await _build_two_action_failing_system()
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused = await wait_for_paused_thread(f.runtime, record.id)

        assert "shaker1" in f.res_mgr.reservations

        f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_METHOD)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED

        assert "shaker1" not in f.res_mgr.reservations, (
            "Reservation should be released after SKIP_METHOD"
        )
        await f.runtime.shutdown()


class TestConcern1FailurePolicyNoAction:
    """CONCERN-1: current_failure_policy should raise when no action is executing.

    Current code (method.py lines 141-144) returns FailurePolicy.PAUSE
    when _current_unresolved_action is None, silently masking a logic error.
    """

    def test_failure_policy_raises_when_no_action(self) -> None:
        """Accessing current_failure_policy with no current action should
        raise ValueError, not silently return PAUSE."""
        method_instance = MethodInstance("test_method")
        event_bus = EventBus()
        status_mgr = StatusManager(event_bus)
        context = WorkflowExecutionContext(
            execution_id="wf-1", workflow_name="test"
        )
        executing = ExecutingMethod(method_instance, event_bus, status_mgr, context)

        with pytest.raises(ValueError):
            _ = executing.current_failure_policy


class TestMultiActionRecovery:
    """Multi-action method: recovery decisions correctly continue execution.

    These tests use a two-action method (shake + seal) to verify that
    skip and retry on the first action allow the second action to execute.
    The retry test specifically catches the event subscription bug identified
    in the review: retry_current_action_in_place() must subscribe the new
    ExecutableLocationAction to the completion event, or _current_action is
    never cleared and the method never completes.
    """

    async def test_skip_first_action_continues_to_second(self) -> None:
        """Shake fails, skip it, seal executes, workflow completes."""
        f = await _build_two_action_failing_system()
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused = await wait_for_paused_thread(f.runtime, record.id)

        f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_ACTION)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.COMPLETED
        assert len(f.device.shake_calls) == 1
        assert f.device.shake_calls[0].succeeded is False
        await f.runtime.shutdown()

    async def test_retry_first_action_then_continues_to_second(self) -> None:
        """Shake fails, retry (succeeds), seal executes, workflow completes.

        This is the critical test for the event subscription bug: if
        retry_current_action_in_place() doesn't subscribe the new action
        to the completion event, _current_action is never cleared, the
        seal action is never resolved, and this test hangs until timeout.
        """
        f = await _build_two_action_failing_system()
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused = await wait_for_paused_thread(f.runtime, record.id)

        f.device.should_fail = False
        f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.COMPLETED
        assert len(f.device.shake_calls) == 2
        assert f.device.shake_calls[0].succeeded is False
        assert f.device.shake_calls[1].succeeded is True
        await f.runtime.shutdown()

    async def test_skip_method_abandons_remaining_actions(self) -> None:
        """Shake fails, skip_method abandons seal too, workflow completes."""
        f = await _build_two_action_failing_system()
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused = await wait_for_paused_thread(f.runtime, record.id)

        f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_METHOD)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.COMPLETED
        assert len(f.device.shake_calls) == 1
        await f.runtime.shutdown()


_DECISION_EVENTS: dict[RecoveryDecision, ThreadEvent] = {
    RecoveryDecision.RETRY: ThreadEvent.RECOVERY_RETRY,
    RecoveryDecision.CONTINUE: ThreadEvent.RECOVERY_SKIP,
    RecoveryDecision.ABORT_ACTION: ThreadEvent.RECOVERY_SKIP,
    RecoveryDecision.ABORT_METHOD: ThreadEvent.RECOVERY_SKIP,
    RecoveryDecision.ABORT_THREAD: ThreadEvent.RECOVERY_SKIP,
}


class TestRecoveryEventMapping:
    """Which thread event each operator decision fires, with no silent default.

    A decision the mapping does not know must raise. Falling through to
    RECOVERY_SKIP would carry the run forward past work that never happened.
    """

    @pytest.mark.parametrize(
        ("decision", "event"),
        list(_DECISION_EVENTS.items()),
        ids=[d.name for d in _DECISION_EVENTS],
    )
    def test_each_action_level_decision_fires_its_mapped_event(
        self, decision: RecoveryDecision, event: ThreadEvent,
    ) -> None:
        assert _recovery_event_for(decision) is event

    def test_retry_op_raises_rather_than_advancing_past_the_action(self) -> None:
        with pytest.raises(ValueError, match="RETRY_OP"):
            _recovery_event_for(RecoveryDecision.RETRY_OP)

    def test_a_new_recovery_decision_must_be_mapped_here_explicitly(self) -> None:
        accounted = set(_DECISION_EVENTS) | {RecoveryDecision.RETRY_OP}
        assert accounted == set(RecoveryDecision)


class TestContinuePastErroredAction:
    """CONTINUE: the action errored, the operator dealt with it, the run carries on.

    Different from ABORT_ACTION, which discards the action as work that never
    happened. CONTINUE is the operator saying "I have seen this and the cell is
    fine" -- so it leaves a record. The action keeps its ERRORED status, the
    ledger gains an operator-confirmed marker, and an ACTION_CONTINUED incident
    says the state past this point was never checked against the device.
    """

    async def test_next_action_runs_after_continue(self) -> None:
        f = await _build_two_action_failing_system()
        await f.runtime.start()

        record = await f.runtime.submit_workflow(
            f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
        )
        paused = await wait_for_paused_thread(f.runtime, record.id)

        f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.CONTINUE)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.COMPLETED
        assert len(f.device.shake_calls) == 1, "the errored action is not re-run"
        assert f.later_action_calls == ["seal"], "the next action runs"
        await f.runtime.shutdown()

    async def test_continue_ledgers_the_action_as_operator_confirmed(self) -> None:
        """The ledger must not be able to read a continued action as a clean run."""
        f = await _build_two_action_failing_system()
        await f.runtime.start()

        record = await f.runtime.submit_workflow(
            f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
        )
        paused = await wait_for_paused_thread(f.runtime, record.id)
        f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.CONTINUE)
        await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        records = await f.runtime.ops_history.list(record.id)
        markers = [
            op for r in records for op in r.operations
            if op.operation == DeviceOperation.ACTION_CONTINUED
        ]
        assert len(markers) == 1, "one marker per continued action"
        marker = markers[0]
        assert marker.source == TrackingSource.OPERATOR
        assert marker.device_name == "shaker1"
        assert isinstance(marker.details, ActionContinuedDetails)
        assert marker.details.command == "shake"
        assert marker.details.error_type == "RuntimeError"
        assert "Simulated shake failure" in marker.details.error_message
        await f.runtime.shutdown()

    async def test_continue_records_an_incident_naming_the_unverified_state(self) -> None:
        f = await _build_two_action_failing_system()
        await f.runtime.start()

        record = await f.runtime.submit_workflow(
            f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
        )
        paused = await wait_for_paused_thread(f.runtime, record.id)
        f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.CONTINUE)
        await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        incidents = await f.runtime.incidents.list(
            category=IncidentCategory.ACTION_CONTINUED,
        )
        assert len(incidents) == 1
        incident = incidents[0]
        assert incident.severity == IncidentSeverity.WARNING
        assert incident.recovery_action == RecoveryAction.NONE
        assert isinstance(incident.detail, ActionContinuedContext)
        assert incident.detail.action_command == "shake"
        assert incident.detail.method_name == "two_action_method"

        # The failure itself stays on the board unacknowledged: continuing past
        # an error is not the same as having resolved it.
        failures = await f.runtime.incidents.list(
            category=IncidentCategory.ACTION_FAILED,
        )
        assert [i.acknowledged for i in failures] == [False]
        await f.runtime.shutdown()

    async def test_abort_action_records_no_operator_confirmation(self) -> None:
        """The other half of the distinction. ABORT_ACTION discards the action as
        work that never happened, so it must leave neither the ledger marker nor
        the incident that say an operator looked at the cell and carried on."""
        f = await _build_two_action_failing_system()
        await f.runtime.start()

        record = await f.runtime.submit_workflow(
            f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
        )
        paused = await wait_for_paused_thread(f.runtime, record.id)
        f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_ACTION)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.COMPLETED
        failures = await f.runtime.incidents.list(
            category=IncidentCategory.ACTION_FAILED,
        )
        assert len(failures) == 1, (
            "control: the action really did fail and the store really is read"
        )
        continued = await f.runtime.incidents.list(
            category=IncidentCategory.ACTION_CONTINUED,
        )
        assert continued == [], "only CONTINUE records an operator confirmation"
        ops = [op for r in await f.runtime.ops_history.list(record.id)
               for op in r.operations]
        assert DeviceOperation.SEAL in [op.operation for op in ops], (
            "control: the ledger really is populated and really is read, so the "
            "absence below is an absence and not an empty store"
        )
        assert DeviceOperation.ACTION_CONTINUED not in [op.operation for op in ops], (
            "an aborted action leaves no operator-confirmed marker in the ledger"
        )
        await f.runtime.shutdown()

    async def test_continue_keeps_the_ops_the_action_really_performed(self) -> None:
        """A device call that returned belongs in the ledger even though the
        action around it never finished. Every other failure path drops the
        action's operation log on the floor; after a CONTINUE the run keeps
        going, so a plate whose history is missing a real op stays wrong for
        the rest of the run."""
        f = await _build_partial_action_failing_system()
        await f.runtime.start()

        record = await f.runtime.submit_workflow(
            f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
        )
        paused = await wait_for_paused_thread(f.runtime, record.id)
        f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.CONTINUE)
        await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        assert f.later_action_calls == ["delid"]
        ops = [op for r in await f.runtime.ops_history.list(record.id)
               for op in r.operations]
        assert DeviceOperation.SEAL in [op.operation for op in ops], (
            "the seal that succeeded before the shake failed must be recorded"
        )
        assert DeviceOperation.SHAKE not in [op.operation for op in ops], (
            "the call that raised did not happen and must not be recorded"
        )
        assert DeviceOperation.WELL_USAGE not in [op.operation for op in ops], (
            "what the action DECLARED it would do is not evidence it happened; "
            "a continued action must never fold its declared tracking"
        )
        await f.runtime.shutdown()

    async def test_a_clean_run_of_the_same_action_does_fold_its_declared_tracking(
        self,
    ) -> None:
        """Control for the assertion above. The declared fold really does fire on
        a clean run, so its absence after a CONTINUE is the continue suppressing
        it, not the fixture never declaring anything."""
        f = await _build_partial_action_failing_system()
        f.device.should_fail = False
        await f.runtime.start()

        record = await f.runtime.submit_workflow(
            f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
        )
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.COMPLETED
        ops = [op for r in await f.runtime.ops_history.list(record.id)
               for op in r.operations]
        assert DeviceOperation.WELL_USAGE in [op.operation for op in ops]
        await f.runtime.shutdown()

    async def test_continue_works_when_the_body_fails_outside_a_device_call(
        self,
    ) -> None:
        """The plain action-error branch, not the operation-recovery seam. An
        action body can raise in its own Python -- a bad variable, missing
        labware, a recipe that names something the deck does not carry -- and
        CONTINUE has to carry the thread on from there too, recording the same
        way."""
        f = await _build_body_failing_system()
        await f.runtime.start()

        record = await f.runtime.submit_workflow(
            f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
        )
        paused = await wait_for_paused_thread(f.runtime, record.id)
        f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.CONTINUE)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.COMPLETED
        assert f.later_action_calls == ["seal"]

        incidents = await f.runtime.incidents.list(
            category=IncidentCategory.ACTION_CONTINUED,
        )
        assert len(incidents) == 1
        assert isinstance(incidents[0].detail, ActionContinuedContext)
        assert incidents[0].detail.error_type == "ValueError"
        assert "reagent" in incidents[0].detail.error_message

        markers = [
            op for r in await f.runtime.ops_history.list(record.id)
            for op in r.operations
            if op.operation == DeviceOperation.ACTION_CONTINUED
        ]
        assert len(markers) == 1
        assert markers[0].source == TrackingSource.OPERATOR
        await f.runtime.shutdown()

    async def test_continue_refused_at_a_move_pause_with_the_labware_still_in_the_jaws(
        self,
    ) -> None:
        """CONTINUE at a move pause says the move is done. It is refused while
        the ledger still has the labware short of the target -- here in the
        gripper, where a failed place leaves it -- and the message names where
        the ledger has it so the operator knows what is left to do."""
        f = await _build_move_failing_system()
        await f.runtime.start()

        record = await f.runtime.submit_workflow(
            f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
        )
        paused = await wait_for_paused_thread(f.runtime, record.id)

        with pytest.raises(ValueError, match="robot1/gripper"):
            f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.CONTINUE)

        f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_THREAD)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        await f.runtime.shutdown()

    async def test_continue_completes_a_move_the_operator_finished_by_hand(
        self,
    ) -> None:
        """The arm dropped the plate mid-place; the operator put it on the
        target themselves and recorded that with edit-location. CONTINUE then
        means what it means everywhere else -- the work is done, carry on -- and
        the run reaches the action the move was feeding without the arm being
        sent at the target a second time."""
        f = await _build_move_failing_system()
        mover = f.transporter
        assert mover is not None
        await f.runtime.start()

        record = await f.runtime.submit_workflow(
            f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
        )
        paused = await wait_for_paused_thread(f.runtime, record.id)
        assert paused.labware_id is not None
        target = mover.place_targets[-1]

        await f.runtime.labware.edit_location(
            paused.labware_id, target, confirm=True,
            reason="Lifted it off the gripper and onto the shaker by hand.",
        )
        # The end-of-thread move home has to be able to complete, or the
        # execution never reaches COMPLETED and the assertions below are moot.
        mover.should_fail_place = False
        f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.CONTINUE)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.COMPLETED
        assert mover.place_targets.count(target) == 1, (
            "CONTINUE must not send the arm at a target the operator already filled"
        )
        assert f.device.shake_calls, "the action the move was feeding never ran"

        incidents = await f.runtime.incidents.list(
            category=IncidentCategory.MOVE_CONTINUED,
        )
        assert len(incidents) == 1
        detail = incidents[0].detail
        assert isinstance(detail, MoveContinuedContext)
        assert detail.target == target
        assert detail.error_type == "RuntimeError"
        await f.runtime.shutdown()

    async def test_continue_releases_the_reservation(self) -> None:
        f = await _build_two_action_failing_system()
        await f.runtime.start()

        record = await f.runtime.submit_workflow(
            f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
        )
        paused = await wait_for_paused_thread(f.runtime, record.id)
        assert "shaker1" in f.res_mgr.reservations

        f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.CONTINUE)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.COMPLETED
        assert "shaker1" not in f.res_mgr.reservations
        await f.runtime.shutdown()


class TestDoublePauseRecovery:
    """Thread can pause, recover, fail again, and recover again.

    This tests both the double-pause cycle AND (indirectly) the event
    subscription bug: if the first retry doesn't properly subscribe, the
    second failure never triggers _handle_action_completed, so the
    method never clears _current_action and loops forever.
    """

    async def test_double_failure_retry_cycle(self) -> None:
        """Shake fails twice (retry both times), then succeeds on third attempt."""
        f = await _build_failing_system(FailurePolicy.PAUSE)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)

        # First failure + retry (still failing)
        paused1 = await wait_for_paused_thread(f.runtime, record.id)
        initial_fail_count = len(f.device.shake_calls)
        f.runtime.recover_thread(record.id, paused1.id, RecoveryDecision.RETRY)

        # Wait for the second failure to register (device still failing)
        deadline = asyncio.get_event_loop().time() + 10.0
        while asyncio.get_event_loop().time() < deadline:
            if len(f.device.shake_calls) > initial_fail_count:
                break
            await asyncio.sleep(0.05)

        # Second failure should have paused again
        paused2 = await wait_for_paused_thread(f.runtime, record.id)
        f.device.should_fail = False
        f.runtime.recover_thread(record.id, paused2.id, RecoveryDecision.RETRY)

        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.COMPLETED
        assert len(f.device.shake_calls) == 3
        assert f.device.shake_calls[0].succeeded is False
        assert f.device.shake_calls[1].succeeded is False
        assert f.device.shake_calls[2].succeeded is True
        await f.runtime.shutdown()


# ---------------------------------------------------------------------------
# Regression guards
# ---------------------------------------------------------------------------

class TestStatusAfterResume:
    """After a paused thread is resumed, it must leave PAUSED status quickly
    enough that a second recover_thread() call is rejected."""

    async def test_status_not_paused_after_recovery(self) -> None:
        f = await _build_failing_system(FailurePolicy.PAUSE)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused = await wait_for_paused_thread(f.runtime, record.id)

        f.device.should_fail = False
        f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY)

        await wait_for_runtime_condition(
            f.runtime,
            lambda: len(f.runtime.get_paused_threads(record.id)) == 0,
            timeout=10.0,
        )

        still_paused = f.runtime.get_paused_threads(record.id)
        assert len(still_paused) == 0, (
            "Thread should not be PAUSED after recovery decision is applied"
        )

        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED
        await f.runtime.shutdown()

    async def test_double_recover_rejected_after_first_processed(self) -> None:
        """A second recover_thread() call is rejected once the thread
        has left PAUSED status."""
        f = await _build_failing_system(FailurePolicy.PAUSE)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused = await wait_for_paused_thread(f.runtime, record.id)

        f.device.should_fail = False
        f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY)

        await wait_for_runtime_condition(
            f.runtime,
            lambda: len(f.runtime.get_paused_threads(record.id)) == 0,
            timeout=10.0,
        )

        with pytest.raises(ValueError, match="not paused"):
            f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_ACTION)

        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED
        await f.runtime.shutdown()


class TestRetryReservationLifecycle:
    """After a successful retry, the reservation is released through the
    normal action completion path. If the completion event subscription
    is missing, the reservation leaks permanently."""

    async def test_retry_releases_reservation_on_normal_completion(self) -> None:
        f = await _build_failing_system()
        await f.runtime.start()

        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused = await wait_for_paused_thread(f.runtime, record.id)

        f.device.should_fail = False
        f.runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED

        assert "shaker1" not in f.res_mgr.reservations, (
            "Reservation should be released after successful retry. "
            "If the completion event subscription is missing, "
            "_handle_action_completed never fires and the reservation leaks."
        )
        await f.runtime.shutdown()


# ---------------------------------------------------------------------------
# Retry re-resolves variables
# ---------------------------------------------------------------------------


@dataclass
class FailingRecordingShakeCall:
    duration: int
    speed: int
    succeeded: bool


class FailingRecordingDevice(UniversalMockDevice):
    """Device that fails once, records all shake arguments."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.should_fail = True
        self.shake_calls: list[FailingRecordingShakeCall] = []

    async def shake(self, duration: int, speed: int) -> None:
        if self.should_fail:
            self.shake_calls.append(FailingRecordingShakeCall(duration, speed, succeeded=False))
            raise RuntimeError("Simulated shake failure")
        await super().shake(duration, speed)
        self.shake_calls.append(FailingRecordingShakeCall(duration, speed, succeeded=True))


async def _build_variable_failing_system() -> tuple["SystemRuntime", "WorkflowTemplate", "FailingRecordingDevice", "EventBus"]:
    """Build system with a Var-parameterized shake that fails on first attempt."""
    from orca.variables import Var, VariableDefinition

    device = FailingRecordingDevice("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.PAUSE)
    async def shake(ctx: object) -> None:
        duration = await ctx.param("shake_time")
        await ctx.device().shake(duration=duration, speed=800)

    async def _var_shake_gen(ctx: object) -> AsyncGenerator[ActionTemplate, None]:
        yield shake

    method = MethodTemplate("var_shake", func=_var_shake_gen)

    pad_loc = system_map.get_location("pad1")

    async def _thread_gen(ctx: object) -> AsyncGenerator[MethodTemplate, None]:
        yield method

    thread = ThreadTemplate(labware_template=plate, start=pad_loc, end=pad_loc,
                            func=_thread_gen)
    workflow = WorkflowTemplate("var_failing_workflow")
    workflow.add_variable("shake_time", VariableDefinition(type="int", default=100))
    workflow.add_thread(thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[plate],
        resources_registry=registry, system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    runtime = SystemRuntime(system, event_bus=event_bus)
    return runtime, workflow, device, event_bus


class TestRetryReResolvesVariables:
    """Action-level RETRY after failure re-resolves variables from the store."""

    async def test_retry_uses_updated_variable_value(self) -> None:
        """Change variable while paused, action-level RETRY picks up the new value."""
        runtime, workflow, device, _ = await _build_variable_failing_system()
        await runtime.start()

        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused = await wait_for_paused_thread(runtime, record.id)

        # First attempt used default=100 and failed
        assert len(device.shake_calls) == 1
        assert device.shake_calls[0].duration == 100
        assert device.shake_calls[0].succeeded is False

        # Change the variable while paused
        entry = runtime._executions[record.id]
        wf_instance_id = entry.system.executing_threads[0].context.execution_id
        entry.system.variable_store.set("shake_time", 9999, wf_instance_id)

        # Action-level RETRY re-runs the whole action, re-resolving to 9999
        device.should_fail = False
        runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED

        assert len(device.shake_calls) == 2
        assert device.shake_calls[1].duration == 9999, (
            f"Expected retry to use updated variable value 9999, "
            f"got {device.shake_calls[1].duration}"
        )
        assert device.shake_calls[1].succeeded is True
        await runtime.shutdown()

    async def test_retry_without_change_uses_same_value(self) -> None:
        """Retry without changing the variable still resolves correctly."""
        runtime, workflow, device, _ = await _build_variable_failing_system()
        await runtime.start()

        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused = await wait_for_paused_thread(runtime, record.id)

        assert device.shake_calls[0].duration == 100

        device.should_fail = False
        runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED

        assert device.shake_calls[1].duration == 100
        assert device.shake_calls[1].succeeded is True
        await runtime.shutdown()

class TestActionFailureSurfacing:
    """A default-PAUSE action failure must be visible to the operator.

    The thread still pauses and waits for a recovery decision (unchanged).
    What is new: the PAUSED event carries the cause, and a queryable
    incident is recorded so `orca incident list` / `GET /api/incidents`
    shows the error without scraping a deployment's stderr.
    """

    async def test_paused_event_carries_last_error(self) -> None:
        f = await _build_failing_system(FailurePolicy.PAUSE)
        collector = CollectorSink()
        f.runtime.register_sink(collector)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(
            f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
        )
        await wait_for_paused_threads(f.runtime, record.id)

        paused_events = [
            e for e in collector.events
            if e.status == "PAUSED" and e.entity_type == "THREAD"
        ]
        assert paused_events, "expected a THREAD PAUSED event"
        ctx = paused_events[-1].context
        assert getattr(ctx, "pause_reason", None) == "error"
        assert "Simulated shake failure" in (getattr(ctx, "last_error", None) or "")

        paused = f.runtime.get_paused_threads(record.id)
        f.runtime.recover_thread(
            record.id, paused[0].id, RecoveryDecision.ABORT_THREAD,
        )
        try:
            await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        except Exception:
            pass
        await f.runtime.shutdown()

    async def test_action_failure_records_incident(self) -> None:
        f = await _build_failing_system(FailurePolicy.PAUSE)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(
            f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
        )
        paused_thread = await wait_for_paused_thread(f.runtime, record.id)

        incidents = await f.runtime.incidents.list(
            category=IncidentCategory.ACTION_FAILED,
        )
        assert len(incidents) == 1, "expected one ACTION_FAILED incident"
        incident = incidents[0]
        assert incident.execution_id == record.id
        assert incident.thread_id == paused_thread.id
        # The action failed inside ctx.device().shake(), so the advised verb is
        # the op-level retry: the whole-action one re-drives the body with no
        # hardware reconcile.
        assert incident.recovery_action == RecoveryAction.THREAD_RECOVER_RETRY_OP
        assert incident.detail.device_command == "shake"
        assert "Simulated shake failure" in incident.message

        f.runtime.recover_thread(
            record.id, paused_thread.id, RecoveryDecision.ABORT_THREAD,
        )
        try:
            await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        except Exception:
            pass
        await f.runtime.shutdown()

    async def test_abort_policy_records_no_incident(self) -> None:
        """ABORT policy re-raises (no pause); the surfacing incident is a
        PAUSE-path concern only, so no ACTION_FAILED incident is recorded."""
        f = await _build_failing_system(FailurePolicy.ABORT)
        await f.runtime.start()

        record = await f.runtime.submit_workflow(
            f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
        )
        try:
            await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        except Exception:
            pass

        incidents = await f.runtime.incidents.list(
            category=IncidentCategory.ACTION_FAILED,
        )
        assert incidents == []
        await f.runtime.shutdown()

    async def test_override_with_pause_records_no_incident(self) -> None:
        """An OverrideWithPauseError (e.g. gateway control) pauses the
        thread but is NOT an action-body failure, so no ACTION_FAILED
        incident is recorded -- otherwise every gateway command racing a
        workflow would spam a bogus failure record."""
        from orca.resource_models.device_error import (
            DeviceUnderExternalControlError,
        )

        f = await _build_failing_system(FailurePolicy.PAUSE)
        f.device.error_factory = lambda: DeviceUnderExternalControlError(
            "shaker1",
        )
        await f.runtime.start()

        record = await f.runtime.submit_workflow(
            f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
        )
        paused_thread = await wait_for_paused_thread(f.runtime, record.id)
        assert paused_thread.status == "PAUSED"

        incidents = await f.runtime.incidents.list(
            category=IncidentCategory.ACTION_FAILED,
        )
        assert incidents == []

        f.runtime.recover_thread(
            record.id, paused_thread.id, RecoveryDecision.ABORT_THREAD,
        )
        try:
            await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        except Exception:
            pass
        await f.runtime.shutdown()


class TestRecoverMutationLock:
    """N3 regression: ThreadFacade.recover takes the per-thread mutation lock
    so a recovery decision cannot interleave with an in-flight mutation's await
    window. Mutations (insert/replace) validate pause state, then await the
    template build while holding the lock before touching the lane; recover
    must wait its turn, or it could unpark the thread mid-build and the
    mutation would land on a lane that is already being consumed."""

    async def test_recover_blocks_while_mutation_lock_held(self) -> None:
        f = await _build_failing_system(FailurePolicy.PAUSE)
        await f.runtime.start()
        record = await f.runtime.submit_workflow(
            f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
        )
        paused = await wait_for_paused_thread(f.runtime, record.id)

        # Stand in for an in-flight mutation holding the per-thread lock.
        lock = f.runtime.threads._lock_for(paused.id)
        await lock.acquire()
        recover_task = asyncio.create_task(
            f.runtime.threads.recover(
                record.id, paused.id, RecoveryDecision.ABORT_THREAD, confirm=True,
            )
        )
        try:
            # One yield advances recover_task to its only await (the lock); it
            # can't complete while the lock is held, so still-pending == blocked.
            await asyncio.sleep(0)
            assert not recover_task.done(), (
                "recover must block on the per-thread mutation lock while a "
                "mutation holds it (N3)"
            )
        finally:
            lock.release()

        # Lock released -> recover proceeds and the thread reaches ABORTED.
        await asyncio.wait_for(recover_task, timeout=10.0)
        try:
            await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
        except Exception:
            pass
        aborted = [t for t in f.runtime.list_threads(record.id) if t.id == paused.id]
        assert aborted and aborted[0].status == "ABORTED"
        await f.runtime.shutdown()
