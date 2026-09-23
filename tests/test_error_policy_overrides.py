"""Integration tests for the OverrideWithPauseError marker mechanism.

Proves that ``OverrideWithPauseError`` subclasses force PAUSE regardless
of the method's declared ``FailurePolicy``.

The marker base lives in
``src/orca/workflow_models/error_policy_overrides.py``. The action-error
handler in ``ExecutingLabwareThread`` checks ``_should_force_pause`` at
every policy-ABORT branch site. These tests drive the production
workflow loop (no inline raises in test bodies) so a regression that
deletes either the marker inheritance OR the override check fails loud.
"""

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import pytest

from orca.resource_models.device_error import (
    DeviceError,
    DeviceUnderExternalControlError,
)
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.events import EventBus
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import ThreadTemplate, WorkflowTemplate
import orca.orca as orca
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.error_policy_overrides import OverrideWithPauseError
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate, MethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.status_enums import FailurePolicy, RecoveryDecision
from orca.runtime.run_modes import WorkflowRunMode

from tests.mock import UniversalMockDevice
from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    wait_for_paused_thread,
    wire_system_map,
)


# ---------------------------------------------------------------------------
# Unit tests: the marker class and the helper
# ---------------------------------------------------------------------------


def test_override_with_pause_error_is_a_normal_exception() -> None:
    """The marker base is just an ``Exception`` -- it can be raised and
    caught with no further machinery. This pins the contract that
    subclasses don't need to pay any tax beyond multiple inheritance."""
    with pytest.raises(OverrideWithPauseError):
        raise OverrideWithPauseError("test")


def test_device_under_external_control_error_is_override_marker() -> None:
    """``DeviceUnderExternalControlError`` opts in to the override
    category by multiple-inheriting ``OverrideWithPauseError``. If a
    refactor accidentally drops the inheritance, the action-error
    handler's override check stops firing and ABORT-policy methods
    again terminate threads on coordination signals.
    """
    err = DeviceUnderExternalControlError(device_name="mlstar_1")
    assert isinstance(err, OverrideWithPauseError)
    # Still a DeviceError -- the device-error catch path stays intact.
    assert isinstance(err, DeviceError)


def test_should_force_pause_recognizes_marker_subclasses() -> None:
    """``_should_force_pause`` is the single helper every policy-ABORT
    branch consults. True for any ``OverrideWithPauseError`` subclass,
    False for everything else (method failures, engine bugs).
    """
    assert ExecutingLabwareThread._should_force_pause(
        DeviceUnderExternalControlError(device_name="x")
    )
    assert ExecutingLabwareThread._should_force_pause(OverrideWithPauseError("x"))
    # Non-marker exceptions stay false. RuntimeError stands in for the
    # method-failure category (Category A in the taxonomy doc); the
    # override must NOT pull these into PAUSE.
    assert not ExecutingLabwareThread._should_force_pause(RuntimeError("driver"))
    assert not ExecutingLabwareThread._should_force_pause(ValueError("bad input"))
    assert not ExecutingLabwareThread._should_force_pause(AssertionError("engine"))


# ---------------------------------------------------------------------------
# Integration tests: drive the real ExecutingLabwareThread loop
# ---------------------------------------------------------------------------


@dataclass
class OverrideFixture:
    """Bundle of objects a single override-integration test needs."""
    runtime: SystemRuntime
    workflow: WorkflowTemplate
    device: UniversalMockDevice


async def _build_external_control_system(policy: FailurePolicy) -> OverrideFixture:
    """Build a single-action workflow whose action targets a device.

    The caller toggles the device's external-control flag before
    submitting the workflow. The action's gate inside
    ``ActionBodyLocationAction.execute()`` raises
    ``DeviceUnderExternalControlError`` when the flag is set; the
    action-error handler then sees the marker and routes through
    PAUSE regardless of ``policy``.
    """
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

    @orca.action(device=pool, inputs=[plate], failure_policy=policy)
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    async def _shake_gen(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shake_action

    method = MethodTemplate("shake_method", func=_shake_gen)
    pad_loc = system_map.get_location("pad1")

    async def _thread_gen(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield method

    thread = ThreadTemplate(
        labware_template=plate,
        start=pad_loc,
        end=pad_loc,
        func=_thread_gen,
    )

    workflow = WorkflowTemplate("override_pause_workflow")
    workflow.add_thread(thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="override_pause_test", description="",
        labwares=[plate], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    runtime = SystemRuntime(builder.get_system(), event_bus=event_bus)
    return OverrideFixture(runtime, workflow, device)


async def test_abort_policy_pauses_on_device_under_external_control() -> None:
    """The regression test: a method declared ``FailurePolicy.ABORT``
    against a device under external control must PAUSE, not ABORT.

    Without the override mechanism this test would land the execution
    at FAILED -- the gate's ``DeviceUnderExternalControlError`` would
    bubble through ``_handle_action_error``'s ABORT branch and
    terminate the thread. With the override the thread parks at
    PAUSED so the operator can release external control and RETRY.
    """
    f = await _build_external_control_system(FailurePolicy.ABORT)
    f.device.take_external_control()
    await f.runtime.start()

    record = await f.runtime.submit_workflow(
        f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
    )
    paused_thread = await wait_for_paused_thread(f.runtime, record.id)

    assert paused_thread.status == "PAUSED", (
        "ABORT-policy method against an externally-controlled device "
        "must PAUSE, not terminate. The override is the contract here."
    )

    # Clean shutdown: operator chooses ABORT_THREAD so the test ends.
    f.runtime.recover_thread(
        record.id, paused_thread.id, RecoveryDecision.ABORT_THREAD,
    )
    try:
        await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
    except Exception:
        pass
    await f.runtime.shutdown()


async def test_pause_policy_pauses_on_device_under_external_control() -> None:
    """Negative: PAUSE policy already PAUSEs on any error, so the
    override is a no-op for it. This test exists to prove the override
    didn't accidentally change PAUSE-policy behavior -- regressions
    that broke the PAUSE path while wiring the override would fail
    this assertion.
    """
    f = await _build_external_control_system(FailurePolicy.PAUSE)
    f.device.take_external_control()
    await f.runtime.start()

    record = await f.runtime.submit_workflow(
        f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
    )
    paused_thread = await wait_for_paused_thread(f.runtime, record.id)
    assert paused_thread.status == "PAUSED"

    f.runtime.recover_thread(
        record.id, paused_thread.id, RecoveryDecision.ABORT_THREAD,
    )
    try:
        await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)
    except Exception:
        pass
    await f.runtime.shutdown()


class _DriverFailingDevice(UniversalMockDevice):
    """Device whose ``shake`` raises a Category-A method failure.

    A plain ``RuntimeError`` is NOT an ``OverrideWithPauseError`` -- it
    represents a normal method failure where the workflow author's
    declared policy should apply.
    """

    def __init__(self, name: str) -> None:
        super().__init__(name)

    async def shake(self, duration: int, speed: int) -> None:
        del duration, speed
        raise RuntimeError("driver shake failed")


async def _build_method_failure_system() -> OverrideFixture:
    """Variant of ``_build_external_control_system`` whose device raises
    a plain ``RuntimeError`` from ``shake``. Used to prove that the
    override does NOT accidentally pull non-marker exceptions into
    PAUSE -- ABORT policy must still terminate on category-A failures.
    """
    device = _DriverFailingDevice("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.ABORT)
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    async def _shake_gen(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shake_action

    method = MethodTemplate("shake_method", func=_shake_gen)
    pad_loc = system_map.get_location("pad1")

    async def _thread_gen(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield method

    thread = ThreadTemplate(
        labware_template=plate,
        start=pad_loc,
        end=pad_loc,
        func=_thread_gen,
    )

    workflow = WorkflowTemplate("override_non_marker_workflow")
    workflow.add_thread(thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="override_non_marker_test", description="",
        labwares=[plate], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    runtime = SystemRuntime(builder.get_system(), event_bus=event_bus)
    return OverrideFixture(runtime, workflow, device)


async def test_abort_policy_still_aborts_on_non_marker_exception() -> None:
    """Negative: a method failure (``RuntimeError`` from the driver) is
    Category A in the taxonomy. ABORT policy applies, and the
    execution lands at FAILED.

    The override check returns False for non-marker exceptions, so the
    ABORT branch fires as before. A regression that accidentally
    routes Category A errors through PAUSE would park the thread here
    and the wait would time out.
    """
    f = await _build_method_failure_system()
    await f.runtime.start()

    record = await f.runtime.submit_workflow(
        f.workflow.name, mode=WorkflowRunMode.PURE_SIM,
    )
    status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

    assert status.status == ExecutionState.FAILED, (
        "ABORT-policy method that raises a non-marker exception (Category A "
        "method failure) must still terminate. The override applies only to "
        "OverrideWithPauseError subclasses."
    )
    await f.runtime.shutdown()
