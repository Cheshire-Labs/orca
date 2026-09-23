"""Tests for Code methods via queue bridge.

Tests the DeviceHandle, ActionRequest, and code method integration
with the existing MergeLane-based execution pipeline.
"""

import asyncio
from typing import ClassVar

import pytest

import orca.orca as orca
from orca.resource_models.devices import Device
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.variables.variable_store import NullVariableResolver
from orca.workflow_models.device_handle import ActionRequest, DeviceHandle
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.status_enums import FailurePolicy, MethodStatus, WorkflowStatus
from tests.mock import UniversalMockDevice, UniversalSimDriver
from tests.test_helpers import (
    execution_outcome,
    wire_system_map,
    create_test_plate_template,
    create_test_transporter,
    wait_until,
)


# ---------------------------------------------------------------------------
# ActionRequest
# ---------------------------------------------------------------------------


class TestActionRequest:

    def test_fields(self) -> None:
        action = ActionRequest(
            device_name="shaker_1",
            command="shake",
            args=(30, 500),
            kwargs={},
        )
        assert action.device_name == "shaker_1"
        assert action.command == "shake"
        assert action.args == (30, 500)
        assert action.kwargs == {}
        assert action.result is None
        assert action.error is None
        assert not action.completion.is_set()


# ---------------------------------------------------------------------------
# DeviceHandle
# ---------------------------------------------------------------------------


class TestDeviceHandle:

    @pytest.mark.asyncio
    async def test_getattr_puts_action_request_on_queue(self) -> None:
        queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
        handle = DeviceHandle("shaker_1", queue)

        async def complete_soon() -> None:
            await wait_until(lambda: not queue.empty(), timeout=5.0)
            action = await queue.get()
            assert action is not None
            assert action.device_name == "shaker_1"
            assert action.command == "shake"
            assert action.args == (30, 500)
            assert action.kwargs == {}
            action.completion.set()

        task = asyncio.create_task(complete_soon())
        await handle.shake(30, 500)
        await task

    @pytest.mark.asyncio
    async def test_blocks_until_completion(self) -> None:
        queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
        handle = DeviceHandle("reader_1", queue)
        completed = False

        async def call_handle() -> None:
            nonlocal completed
            await handle.read("protocol.prt", "output.csv")
            completed = True

        task = asyncio.create_task(call_handle())
        # The handle enqueues the action before blocking on completion; once it
        # is on the queue, call_handle has reached the block and completed stays False.
        await wait_until(lambda: not queue.empty(), timeout=5.0)
        assert not completed, "DeviceHandle should block until completion signaled"

        action = await queue.get()
        assert action is not None
        action.result = {"absorbance": [0.5, 0.6]}
        action.completion.set()
        await task
        assert completed

    @pytest.mark.asyncio
    async def test_returns_result(self) -> None:
        queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
        handle = DeviceHandle("reader_1", queue)

        async def complete_with_result() -> None:
            action = await queue.get()
            assert action is not None
            action.result = {"data": [1, 2, 3]}
            action.completion.set()

        task = asyncio.create_task(complete_with_result())
        result = await handle.read("abs.prt", "out.csv")
        assert result == {"data": [1, 2, 3]}
        await task

    @pytest.mark.asyncio
    async def test_error_propagates(self) -> None:
        queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
        handle = DeviceHandle("shaker_1", queue)

        async def complete_with_error() -> None:
            action = await queue.get()
            assert action is not None
            action.error = RuntimeError("Device fault")
            action.completion.set()

        task = asyncio.create_task(complete_with_error())
        with pytest.raises(RuntimeError, match="Device fault"):
            await handle.shake(30, 500)
        await task

    @pytest.mark.asyncio
    async def test_kwargs_passed_through(self) -> None:
        queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
        handle = DeviceHandle("shaker_1", queue)

        async def check_and_complete() -> None:
            action = await queue.get()
            assert action is not None
            assert action.kwargs == {"duration": 30, "speed": 500}
            action.completion.set()

        task = asyncio.create_task(check_and_complete())
        await handle.shake(duration=30, speed=500)
        await task

    @pytest.mark.asyncio
    async def test_multiple_sequential_calls(self) -> None:
        queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
        handle = DeviceHandle("lh_1", queue)
        commands: list[str] = []

        async def process_actions() -> None:
            for _ in range(3):
                action = await queue.get()
                assert action is not None
                commands.append(action.command)
                action.completion.set()

        task = asyncio.create_task(process_actions())
        await handle.aspirate([20], ["A1"])
        await handle.dispense([20], ["A1"])
        await handle.drop_tips()
        await task

        assert commands == ["aspirate", "dispense", "drop_tips"]

    @pytest.mark.asyncio
    async def test_different_devices_same_queue(self) -> None:
        queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
        shaker = DeviceHandle("shaker_1", queue)
        reader = DeviceHandle("reader_1", queue)
        devices: list[str] = []

        async def process_actions() -> None:
            for _ in range(2):
                action = await queue.get()
                assert action is not None
                devices.append(action.device_name)
                action.completion.set()

        task = asyncio.create_task(process_actions())
        await shaker.shake(30, 500)
        await reader.read("abs.prt")
        await task

        assert devices == ["shaker_1", "reader_1"]


# ---------------------------------------------------------------------------
# MethodContext
# ---------------------------------------------------------------------------


class TestMethodContext:

    def _make_ctx(
        self,
        labware: dict[str, object] | None = None,
    ) -> tuple[MethodContext, asyncio.Queue[ActionRequest | None]]:
        queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
        ctx = MethodContext(
            action_queue=queue,
            assigned_labware=labware or {},
            variable_store=NullVariableResolver(),
            execution_id="test-exec-1",
        )
        return ctx, queue

    @pytest.mark.asyncio
    async def test_device_returns_device_handle(self) -> None:
        ctx, queue = self._make_ctx()
        handle = ctx.device("shaker_1")
        assert isinstance(handle, DeviceHandle)

    @pytest.mark.asyncio
    async def test_device_handle_uses_shared_queue(self) -> None:
        ctx, queue = self._make_ctx()
        handle = ctx.device("shaker_1")

        async def complete() -> None:
            action = await queue.get()
            assert action is not None
            action.completion.set()

        task = asyncio.create_task(complete())
        await handle.shake(30, 500)
        await task
        # If we got here, the handle used the ctx's queue

    @pytest.mark.asyncio
    async def test_different_devices_share_queue(self) -> None:
        ctx, queue = self._make_ctx()
        h1 = ctx.device("shaker_1")
        h2 = ctx.device("reader_1")
        devices: list[str] = []

        async def process() -> None:
            for _ in range(2):
                action = await queue.get()
                assert action is not None
                devices.append(action.device_name)
                action.completion.set()

        task = asyncio.create_task(process())
        await h1.shake(30, 500)
        await h2.read("abs.prt")
        await task
        assert devices == ["shaker_1", "reader_1"]

    def test_labware_returns_instance(self) -> None:
        fake_plate = object()
        ctx, _ = self._make_ctx(labware={"plate_1": fake_plate})
        assert ctx.labware("plate_1") is fake_plate

    def test_labware_missing_raises(self) -> None:
        ctx, _ = self._make_ctx()
        with pytest.raises(ValueError, match="not assigned"):
            ctx.labware("nonexistent")

    async def test_param_delegates_to_variable_store(self) -> None:
        from orca.variables.errors import UndefinedVariableError
        ctx, _ = self._make_ctx()
        with pytest.raises(UndefinedVariableError):
            await ctx.param("undefined_param")


# ---------------------------------------------------------------------------
# Integration tests: code method through full system
# ---------------------------------------------------------------------------


from dataclasses import dataclass
from orca.sdk.labware import PlateTemplate
from orca.runtime.run_modes import WorkflowRunMode


@dataclass
class TestTopology:
    device: Device
    pool: ResourcePool
    plate: PlateTemplate
    registry: ResourceRegistry
    system_map: SystemMap


async def _make_topology(device: Device | None = None) -> TestTopology:
    """Create a minimal topology with one device, one transporter, one plate."""
    if device is None:
        device = UniversalMockDevice("device1")
    transporter = create_test_transporter("robot1", [device.name, "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool(device.name, [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={device.name: device}, pads=["pad1"])

    return TestTopology(device=device, pool=pool, plate=plate,
                        registry=registry, system_map=system_map)


async def _build_system(
    topo: TestTopology,
    method_template: MethodTemplate,
) -> tuple[SystemRuntime, WorkflowTemplate]:
    """Build a runnable system from a topology and method template."""
    pad_loc = topo.system_map.get_location("pad1")

    async def _thread_gen(ctx):
        yield method_template

    thread = ThreadTemplate(
        labware_template=topo.plate,
        start=pad_loc,
        end=pad_loc,
        func=_thread_gen,
    )

    workflow = WorkflowTemplate("code_method_test")
    workflow.add_thread(thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="",
        labwares=[topo.plate], resources_registry=topo.registry,
        system_map=topo.system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    runtime = SystemRuntime(system, event_bus=event_bus)
    return runtime, workflow


class ValueReturningMockDevice(Device):
    """Mock device whose measure() returns a dict with data."""

    KIND: ClassVar[str] = "mock"

    def __init__(self, name: str) -> None:
        driver = UniversalSimDriver(name)
        super().__init__(name, driver, driver)

    async def measure(self, protocol: str) -> dict[str, list[float]]:
        return {"absorbance": [0.5, 0.6, 0.7]}

    async def shake(self, duration: int, speed: int) -> None:
        await self.driver.shake(speed, duration)


class TestCodeMethodIntegration:

    @pytest.mark.asyncio
    async def test_single_action_code_method_executes(self) -> None:
        """A code method with one shake action runs end-to-end."""
        topo = await _make_topology()

        @orca.action(device=topo.pool, inputs=[topo.plate])
        async def shake_it(ctx):
            await ctx.device().shake(duration=1, speed=500)

        @orca.method
        async def shake_method(ctx):
            yield shake_it

        runtime, workflow = await _build_system(topo, shake_method)
        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)

        assert status.status == "completed", f"Expected completed, got {status.status}: {status.error}"
        await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_result_flows_back_from_device(self) -> None:
        """Device return values propagate back through DeviceHandle to user code.

        BUG #1: _handle_action_completed_async signals completion but never
        copies GenericLocationAction._result back to ActionRequest.result.
        """
        captured_result: list[object] = []
        topo = await _make_topology(device=ValueReturningMockDevice("device1"))

        @orca.action(device=topo.pool, inputs=[topo.plate])
        async def read_it(ctx):
            result = await ctx.device().measure("abs_protocol")
            captured_result.append(result)

        @orca.method
        async def read_method(ctx):
            yield read_it

        runtime, workflow = await _build_system(topo, read_method)
        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)

        assert status.status == "completed", f"Expected completed, got {status.status}: {status.error}"
        assert len(captured_result) == 1
        assert captured_result[0] == {"absorbance": [0.5, 0.6, 0.7]}, (
            f"Expected device result, got {captured_result[0]}"
        )
        await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_multi_action_code_method(self) -> None:
        """Code method with multiple sequential actions."""
        topo = await _make_topology()
        call_log: list[str] = []

        @orca.action(device=topo.pool, inputs=[topo.plate])
        async def shake_1(ctx):
            await ctx.device().shake(duration=1, speed=500)
            call_log.append("shake")

        @orca.action(device=topo.pool, inputs=[topo.plate])
        async def shake_2(ctx):
            await ctx.device().shake(duration=2, speed=300)
            call_log.append("shake2")

        @orca.action(device=topo.pool, inputs=[topo.plate])
        async def shake_3(ctx):
            await ctx.device().shake(duration=3, speed=100)
            call_log.append("shake3")

        @orca.method
        async def multi_step(ctx):
            yield shake_1
            yield shake_2
            yield shake_3

        runtime, workflow = await _build_system(topo, multi_step)
        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)

        assert status.status == "completed", f"Expected completed, got {status.status}: {status.error}"
        assert call_log == ["shake", "shake2", "shake3"]
        await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_code_method_user_error_propagates(self) -> None:
        """Exception in user function surfaces as the FAILED execution error."""
        topo = await _make_topology()

        @orca.action(device=topo.pool, inputs=[topo.plate], failure_policy=FailurePolicy.ABORT)
        async def failing_action(ctx):
            raise ValueError("bad data from instrument")

        @orca.method
        async def failing_method(ctx):
            yield failing_action

        runtime, workflow = await _build_system(topo, failing_method)
        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=10.0)

        assert status.status == "failed", (
            f"Expected terminal FAILED, got {status.status}"
        )
        assert status.error is not None
        assert "bad data from instrument" in status.error, (
            f"User error message must propagate; got {status.error!r}"
        )
        await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_abort_code_method_cancels_user_task(self) -> None:
        """Aborting a code method cancels the running user function task.

        BUG #2: abort() closes the lane and sets completed, but never
        cancels _user_task. The user function leaks as a zombie task.
        """
        topo = await _make_topology()
        entered = asyncio.Event()
        task_was_cancelled = asyncio.Event()

        @orca.action(device=topo.pool, inputs=[topo.plate])
        async def blocking_action(ctx):
            await ctx.device().shake(duration=1, speed=500)
            entered.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                task_was_cancelled.set()
                raise

        @orca.method
        async def blocking_method(ctx):
            yield blocking_action

        runtime, workflow = await _build_system(topo, blocking_method)
        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)

        # Wait for the first action to complete and user func to reach sleep
        await asyncio.wait_for(entered.wait(), timeout=10.0)

        # Find the executing method and abort it
        exec_entry = runtime._executions[submission.execution_id]
        threads = exec_entry.system.executing_threads
        assert len(threads) >= 1
        thread = threads[0]
        current_method = thread._assigned_method
        assert current_method is not None
        await current_method.abort()

        # Verify user task was cancelled and status is correct
        await asyncio.wait_for(task_was_cancelled.wait(), timeout=2.0)
        assert current_method.was_aborted
        assert current_method.status == MethodStatus.PARTIAL_COMPLETE
        await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_workflow_status_transitions(self) -> None:
        """ExecutingWorkflow.status transitions through CREATED -> IN_PROGRESS -> COMPLETED."""
        topo = await _make_topology()

        @orca.action(device=topo.pool, inputs=[topo.plate])
        async def quick_shake(ctx):
            await ctx.device().shake(duration=1, speed=500)

        @orca.method
        async def shake_method(ctx):
            yield quick_shake

        runtime, workflow = await _build_system(topo, shake_method)
        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)

        status = await execution_outcome(runtime, submission, timeout=10.0)
        assert status.status == "completed"

        # Verify the ExecutingWorkflow object has correct status
        exec_entry = runtime._executions[submission.execution_id]
        assert exec_entry.executing_workflow is not None
        assert exec_entry.executing_workflow.status == WorkflowStatus.COMPLETED

        await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_workflow_status_errored_on_abort(self) -> None:
        """ExecutingWorkflow.status is ERRORED when a thread aborts."""
        topo = await _make_topology()

        @orca.action(device=topo.pool, inputs=[topo.plate], failure_policy=FailurePolicy.ABORT)
        async def failing_action(ctx):
            await ctx.device().nonexistent_method()

        @orca.method
        async def failing_method(ctx):
            yield failing_action

        runtime, workflow = await _build_system(topo, failing_method)
        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)

        status = await execution_outcome(runtime, submission, timeout=10.0)
        assert status.status == "failed"

        exec_entry = runtime._executions[submission.execution_id]
        assert exec_entry.executing_workflow is not None
        assert exec_entry.executing_workflow.status == WorkflowStatus.ERRORED

        await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_list_reservations_during_execution(self) -> None:
        """SystemRuntime.list_reservations() returns active reservations."""
        from orca.runtime.status_models import ReservationSnapshot

        topo = await _make_topology()
        reservations_captured: list[ReservationSnapshot] = []

        @orca.action(device=topo.pool, inputs=[topo.plate])
        async def slow_shake(ctx):
            await ctx.device().shake(duration=1, speed=500)

        @orca.method
        async def shake_method(ctx):
            yield slow_shake

        runtime, workflow = await _build_system(topo, shake_method)
        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)

        # Poll until we see at least one reservation (action being resolved)
        deadline = asyncio.get_event_loop().time() + 10.0
        while asyncio.get_event_loop().time() < deadline:
            reservations_captured = runtime.list_reservations(submission.execution_id)
            if reservations_captured:
                break
            await asyncio.sleep(0.05)

        await execution_outcome(runtime, submission, timeout=10.0)

        assert len(reservations_captured) > 0, "Should capture at least one reservation during execution"
        for r in reservations_captured:
            assert isinstance(r, ReservationSnapshot)
            assert r.position_id
            assert r.reservation_id

        await runtime.shutdown()
