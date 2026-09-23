"""End-to-end: ad-hoc device-targeting actions/methods injected via the
insert_action / insert_method Operations compile against the LIVE topology
and execute on the real device.

The whole point of the feature: an operator can inject ANY orca.action,
written exactly as in a workflow file (``topology.device(name, Iface)``),
into a running thread and have it reserve + run on the real device. These
tests drive the production wire path (the Operation -> compile_*_code with
LiveTopology(runtime.system) -> facade insert -> execute), not the raw
in-process system call, so they prove the injected source string actually
binds and executes.
"""

import asyncio
import textwrap
from typing import Any, AsyncGenerator, AsyncIterator

import pytest
import pytest_asyncio

import orca.orca as orca
from orca.operations._protocol import OperationError
from orca.operations.thread import (
    InsertActionOperation,
    InsertMethodOperation,
    ReplaceActionOperation,
    ReplaceMethodOperation,
)
from orca.operations.thread_models import (
    InsertActionRequest,
    InsertMethodRequest,
    ReplaceActionRequest,
    ReplaceMethodRequest,
)
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.method_template import IMethodTemplate, MethodTemplate
from orca.workflow_models.status_enums import FailurePolicy, RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import ThreadTemplate
from tests.mock import UniversalMockDevice
from tests.mutation_helpers import wait_for_paused
from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    wire_system_map,
)


class _RecordingDevice(UniversalMockDevice):
    """Records every device command and can fail the first shake so the
    thread error-pauses with its method IN_PROGRESS (the state
    insert_action targets)."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.calls: list[str] = []
        self.fail_first_shake = False
        self._shakes = 0

    async def shake(self, duration: int, speed: int) -> None:
        self._shakes += 1
        if self.fail_first_shake and self._shakes == 1:
            raise RuntimeError("simulated first-shake failure")
        self.calls.append("shake")
        await super().shake(duration, speed)

    async def seal(self, temperature: int, duration: float) -> None:
        self.calls.append("seal")
        await super().seal(temperature, duration)

    async def centrifuge(self, g: int, duration: int) -> None:
        self.calls.append("centrifuge")
        await super().centrifuge(g, duration)

    async def read(self, protocol_filepath: str, output_filepath: str) -> None:
        self.calls.append("read")
        await super().read(protocol_filepath, output_filepath)

    async def delid(self) -> None:
        self.calls.append("delid")
        await super().delid()


async def _build(fail_first_shake: bool = True) -> tuple[SystemRuntime, WorkflowTemplate, _RecordingDevice]:
    device = _RecordingDevice("device1")
    device.fail_first_shake = fail_first_shake
    transporter = create_test_transporter("robot1", ["device1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("device1", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.PAUSE)
    async def shake_action(ctx: Any) -> None:
        await ctx.device().shake(duration=1, speed=500)

    async def _m(ctx: Any) -> AsyncGenerator[Any, None]:
        yield shake_action
    method = MethodTemplate("shake_method", func=_m)

    pad = system_map.get_location("pad1")

    async def _t(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield method
    thread = ThreadTemplate(labware_template=plate, start=pad, end=pad, func=_t)

    workflow = WorkflowTemplate("test_workflow")
    workflow.add_thread(thread, is_start=True)
    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="s", description="", labwares=[plate],
        resources_registry=registry, system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    runtime = SystemRuntime(builder.get_system(), event_bus=event_bus)
    return runtime, workflow, device


async def _build_two_action(fail_first_shake: bool = True) -> tuple[SystemRuntime, WorkflowTemplate, _RecordingDevice]:
    """A method of two actions: a shake that fails, then a centrifuge. The
    second action is what shows where an injected step lands relative to the
    work the method has left."""
    device = _RecordingDevice("device1")
    device.fail_first_shake = fail_first_shake
    transporter = create_test_transporter("robot1", ["device1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("device1", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.PAUSE)
    async def shake_action(ctx: Any) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.PAUSE)
    async def spin_action(ctx: Any) -> None:
        await ctx.device().centrifuge(g=500, duration=60)

    async def _m(ctx: Any) -> AsyncGenerator[Any, None]:
        yield shake_action
        yield spin_action
    method = MethodTemplate("shake_then_spin", func=_m)

    pad = system_map.get_location("pad1")

    async def _t(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield method
    thread = ThreadTemplate(labware_template=plate, start=pad, end=pad, func=_t)

    workflow = WorkflowTemplate("two_action_workflow")
    workflow.add_thread(thread, is_start=True)
    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="s", description="", labwares=[plate],
        resources_registry=registry, system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    runtime = SystemRuntime(builder.get_system(), event_bus=event_bus)
    return runtime, workflow, device


async def _pause_on_shake_failure(runtime: SystemRuntime, workflow: WorkflowTemplate) -> tuple[str, str]:
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    paused_id = await wait_for_paused(runtime, record.id)
    return record.id, paused_id


_Built = tuple[SystemRuntime, WorkflowTemplate, _RecordingDevice]


@pytest_asyncio.fixture
async def built() -> AsyncIterator[_Built]:
    """Built runtime/workflow/device, torn down even if an assertion fails
    so a failing test can't leak the runtime's background tasks into the
    next one."""
    runtime, workflow, device = await _build(fail_first_shake=True)
    try:
        yield runtime, workflow, device
    finally:
        await runtime.shutdown()


_SEAL_SRC = textwrap.dedent("""
    from orca.devices.device_interfaces import ISealer
    from orca.sdk.labware import AnyLabwareTemplate

    @orca.action(device=topology.device("device1", ISealer), inputs=[AnyLabwareTemplate()])
    async def ad_hoc_seal(ctx):
        await ctx.device().seal(temperature=170, duration=3.0)
""")


class TestAdHocActionInjectionEndToEnd:

    async def test_injected_device_action_reserves_and_executes(self, built: _Built) -> None:
        runtime, workflow, device = built
        execution_id, paused_id = await _pause_on_shake_failure(runtime, workflow)

        op = InsertActionOperation(runtime=runtime)
        await op.run(InsertActionRequest(
            execution_id=execution_id, thread_id=paused_id,
            action_code=_SEAL_SRC, where="tail", reason="operator inserts seal",
        ))

        device.fail_first_shake = False
        runtime.recover_thread(execution_id, paused_id, RecoveryDecision.RETRY)
        status = await asyncio.wait_for(runtime.wait(execution_id), timeout=15.0)

        assert status.status == ExecutionState.COMPLETED
        assert "shake" in device.calls
        assert "seal" in device.calls, (
            f"injected ad-hoc seal must execute on the real device; calls={device.calls}"
        )

    @pytest.mark.parametrize(
        "iface,call,marker",
        [
            ("ISealer", "await ctx.device().seal(temperature=170, duration=3.0)", "seal"),
            ("ICentrifuge", "await ctx.device().centrifuge(g=500, duration=60)", "centrifuge"),
            ("IReader", "await ctx.device().read('qc.pro', 'out.csv')", "read"),
            ("IDelidder", "await ctx.device().delid()", "delid"),
        ],
    )
    async def test_many_device_interfaces_inject_and_execute(
        self, built: _Built, iface: str, call: str, marker: str,
    ) -> None:
        runtime, workflow, device = built
        execution_id, paused_id = await _pause_on_shake_failure(runtime, workflow)

        src = textwrap.dedent(f"""
            from orca.devices.device_interfaces import {iface}
            from orca.sdk.labware import AnyLabwareTemplate

            @orca.action(device=topology.device("device1", {iface}), inputs=[AnyLabwareTemplate()])
            async def ad_hoc(ctx):
                {call}
        """)
        op = InsertActionOperation(runtime=runtime)
        await op.run(InsertActionRequest(
            execution_id=execution_id, thread_id=paused_id,
            action_code=src, where="tail", reason=f"inject {marker}",
        ))

        device.fail_first_shake = False
        runtime.recover_thread(execution_id, paused_id, RecoveryDecision.RETRY)
        status = await asyncio.wait_for(runtime.wait(execution_id), timeout=15.0)

        assert status.status == ExecutionState.COMPLETED
        assert marker in device.calls, (
            f"injected {iface} action ({marker}) must execute; calls={device.calls}"
        )

    async def test_injected_method_with_device_action_executes(self, built: _Built) -> None:
        runtime, workflow, device = built
        execution_id, paused_id = await _pause_on_shake_failure(runtime, workflow)

        method_src = textwrap.dedent("""
            from orca.devices.device_interfaces import ISealer
            from orca.sdk.labware import AnyLabwareTemplate

            @orca.action(device=topology.device("device1", ISealer), inputs=[AnyLabwareTemplate()])
            async def ad_hoc_seal(ctx):
                await ctx.device().seal(temperature=170, duration=3.0)

            @orca.method
            async def ad_hoc_method(ctx):
                yield ad_hoc_seal
        """)
        op = InsertMethodOperation(runtime=runtime)
        await op.run(InsertMethodRequest(
            execution_id=execution_id, thread_id=paused_id,
            method_code=method_src, where="tail", reason="inject method",
        ))

        device.fail_first_shake = False
        runtime.recover_thread(execution_id, paused_id, RecoveryDecision.RETRY)
        status = await asyncio.wait_for(runtime.wait(execution_id), timeout=15.0)

        assert status.status == ExecutionState.COMPLETED
        assert "seal" in device.calls, (
            f"injected ad-hoc method's device action must execute; calls={device.calls}"
        )


class TestAdHocReplaceInjectionEndToEnd:
    """replace_action / replace_method also compile injected source with the
    live topology, so an ad-hoc device-targeting replacement reserves and
    executes. Replacing the error-paused current target stages the
    replacement; recover_thread(ABORT_*) drops the failed step and runs it."""

    async def test_replace_action_with_adhoc_device_action_executes(self, built: _Built) -> None:
        runtime, workflow, device = built
        execution_id, paused_id = await _pause_on_shake_failure(runtime, workflow)

        # Replace the errored "shake_action" with an ad-hoc device seal.
        op = ReplaceActionOperation(runtime=runtime)
        await op.run(ReplaceActionRequest(
            execution_id=execution_id, thread_id=paused_id,
            target_command="shake_action", action_code=_SEAL_SRC,
            reason="replace failed shake with seal",
        ))

        # Device left failing: proves the replacement ran, not a retry.
        runtime.recover_thread(execution_id, paused_id, RecoveryDecision.ABORT_ACTION)
        status = await asyncio.wait_for(runtime.wait(execution_id), timeout=15.0)

        assert status.status == ExecutionState.COMPLETED
        assert "shake" not in device.calls
        assert "seal" in device.calls, (
            f"ad-hoc replacement action must execute on the device; calls={device.calls}"
        )

    async def test_replace_method_with_adhoc_device_method_executes(self, built: _Built) -> None:
        runtime, workflow, device = built
        execution_id, paused_id = await _pause_on_shake_failure(runtime, workflow)

        method_src = textwrap.dedent("""
            from orca.devices.device_interfaces import ISealer
            from orca.sdk.labware import AnyLabwareTemplate

            @orca.action(device=topology.device("device1", ISealer), inputs=[AnyLabwareTemplate()])
            async def ad_hoc_seal(ctx):
                await ctx.device().seal(temperature=170, duration=3.0)

            @orca.method
            async def replacement_method(ctx):
                yield ad_hoc_seal
        """)
        op = ReplaceMethodOperation(runtime=runtime)
        await op.run(ReplaceMethodRequest(
            execution_id=execution_id, thread_id=paused_id,
            target_name="shake_method", method_code=method_src,
            reason="replace failed method",
        ))

        runtime.recover_thread(execution_id, paused_id, RecoveryDecision.ABORT_METHOD)
        status = await asyncio.wait_for(runtime.wait(execution_id), timeout=15.0)

        assert status.status == ExecutionState.COMPLETED
        assert "seal" in device.calls, (
            f"ad-hoc replacement method's device action must execute; calls={device.calls}"
        )


class TestAdHocInjectionFailureModes:

    async def test_unknown_device_rejected_as_invalid_input(self, built: _Built) -> None:
        runtime, workflow, device = built
        execution_id, paused_id = await _pause_on_shake_failure(runtime, workflow)

        src = textwrap.dedent("""
            from orca.devices.device_interfaces import ISealer
            from orca.sdk.labware import AnyLabwareTemplate

            @orca.action(device=topology.device("ghost_device", ISealer), inputs=[AnyLabwareTemplate()])
            async def ad_hoc(ctx):
                await ctx.device().seal(temperature=1, duration=1.0)
        """)
        op = InsertActionOperation(runtime=runtime)
        with pytest.raises(OperationError):
            await op.run(InsertActionRequest(
                execution_id=execution_id, thread_id=paused_id,
                action_code=src, where="tail", reason="bad device",
            ))

    async def test_wrong_device_type_rejected_as_invalid_input(self, built: _Built) -> None:
        runtime, workflow, device = built
        execution_id, paused_id = await _pause_on_shake_failure(runtime, workflow)

        src = textwrap.dedent("""
            from orca.resource_models.transporter import Transporter
            from orca.sdk.labware import AnyLabwareTemplate

            @orca.action(device=topology.device("device1", Transporter), inputs=[AnyLabwareTemplate()])
            async def ad_hoc(ctx):
                pass
        """)
        op = InsertActionOperation(runtime=runtime)
        with pytest.raises(OperationError):
            await op.run(InsertActionRequest(
                execution_id=execution_id, thread_id=paused_id,
                action_code=src, where="tail", reason="wrong type",
            ))


@pytest.mark.timeout(20)
async def test_head_insert_plus_abort_action_runs_the_injected_step_instead() -> None:
    """Insert and recover are one move, and the decision is what runs the insert.

    An operator who wants a corrective step INSTEAD of the failed action injects
    it at the head and answers the pause with ABORT_ACTION: the failed action is
    discarded, the injected step is the next thing the method does, and the rest
    of the method still follows it. The insert alone runs nothing -- the thread
    is parked, and only a recovery decision un-parks it.
    """
    runtime, workflow, device = await _build_two_action(fail_first_shake=True)
    try:
        execution_id, paused_id = await _pause_on_shake_failure(runtime, workflow)

        op = InsertActionOperation(runtime=runtime)
        await op.run(InsertActionRequest(
            execution_id=execution_id, thread_id=paused_id,
            action_code=_SEAL_SRC, where="head",
            reason="operator inserts a corrective seal",
        ))
        assert device.calls == [], "an insert must not run anything on its own"

        runtime.recover_thread(execution_id, paused_id, RecoveryDecision.ABORT_ACTION)
        status = await asyncio.wait_for(runtime.wait(execution_id), timeout=15.0)

        assert status.status == ExecutionState.COMPLETED
        assert device.calls == ["seal", "centrifuge"], (
            "the injected action must run next and the method's remaining action "
            f"after it, with the aborted shake discarded; calls={device.calls}"
        )
    finally:
        await runtime.shutdown()
