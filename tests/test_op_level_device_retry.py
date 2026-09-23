"""Operation-level device retry (RETRY_OP): a RETRY_OP recovery decision on a
failed device call re-runs ONLY that call, with the action-body coroutine still
suspended at its await; it does NOT re-run device ops that already succeeded
earlier in the same action body. RETRY_OP is scoped to non-shared actions and is
valid only while a device op is paused (shared-action op-level recovery lands
with the rendezvous redesign).

Discriminator: an action does op1 (always succeeds) then op2 (fails once). After
RETRY_OP, action-level RETRY would re-run the whole body (op1 called twice);
operation-level RETRY_OP re-runs only op2 (op1 called once).
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

from orca.events.execution_context import ExecutionContext
from orca.runtime.incident_store import IncidentCategory, RecoveryAction
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.system.reservation_manager.errors import ActionFailedContext
from orca.resource_models.resource_pool import ResourcePool
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
import orca.orca as orca
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.status_enums import FailurePolicy, RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext

from tests.mock import UniversalMockDevice
from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    wait_for_paused_thread,
    wire_system_map,
)


class TwoOpDevice(UniversalMockDevice):
    """shake() (op1) always succeeds; seal() (op2) fails its first call, then
    succeeds. Each is counted so a test can tell which ops re-ran."""

    def __init__(self, name: str, fail_times: int = 1) -> None:
        super().__init__(name)
        self.op1_calls = 0
        self.op2_calls = 0
        self._op2_fail_remaining = fail_times

    async def shake(self, duration: int, speed: int) -> None:
        self.op1_calls += 1
        await super().shake(duration, speed)

    async def seal(self, temperature: int, duration: float) -> None:
        self.op2_calls += 1
        if self._op2_fail_remaining > 0:
            self._op2_fail_remaining -= 1
            raise RuntimeError("Simulated op2 (seal) failure")
        await super().seal(temperature, duration)


async def _build_two_op_system(fail_times: int = 1) -> tuple[SystemRuntime, WorkflowTemplate, TwoOpDevice]:
    device = TwoOpDevice("dev1", fail_times=fail_times)
    transporter = create_test_transporter("robot1", ["dev1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("dev1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"dev1": device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.PAUSE)
    async def two_op_action(ctx: ActionContext) -> None:
        """Shake the plate, then seal it."""
        await ctx.device().shake(duration=1, speed=500)          # op1: succeeds
        await ctx.device().seal(temperature=180, duration=3)     # op2: fails once

    async def _method_gen(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield two_op_action

    method = MethodTemplate("two_op_method", func=_method_gen)
    pad = system_map.get_location("pad1")

    async def _thread_gen(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        del ctx
        yield method

    thread = ThreadTemplate(labware_template=plate, start=pad, end=pad, func=_thread_gen)
    workflow = WorkflowTemplate("two_op_workflow")
    workflow.add_thread(thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[plate],
        resources_registry=registry, system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    runtime = SystemRuntime(builder.get_system(), event_bus=event_bus)
    return runtime, workflow, device


async def test_retry_reruns_only_failed_op_not_prior_ops() -> None:
    runtime, workflow, device = await _build_two_op_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        paused = await wait_for_paused_thread(runtime, record.id)
        # op1 ran + succeeded; op2 failed, pausing the thread.
        assert device.op1_calls == 1
        assert device.op2_calls == 1

        runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY_OP)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.COMPLETED
        assert device.op1_calls == 1, (
            "operation-level retry must re-run ONLY the failed op; op1 already "
            f"succeeded and must not run again (got {device.op1_calls} calls)"
        )
        assert device.op2_calls == 2, (
            f"the failed op must re-run once (got {device.op2_calls} calls)"
        )
    finally:
        await runtime.shutdown()


async def test_action_level_retry_at_op_pause_reruns_whole_action() -> None:
    """Contrast to RETRY_OP: an action-level RETRY chosen while a device op is
    paused re-runs the ENTIRE action body (via OperationDecisionSignal), so the
    already-succeeded op1 runs a second time. This pins the seam's action-level
    branch, which otherwise only ABORT_THREAD exercises.
    """
    runtime, workflow, device = await _build_two_op_system(fail_times=1)
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        paused = await wait_for_paused_thread(runtime, record.id)
        assert device.op1_calls == 1
        assert device.op2_calls == 1

        runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)

        assert status.status == ExecutionState.COMPLETED
        assert device.op1_calls == 2, (
            "action-level RETRY must re-run the whole action body, so the "
            f"already-succeeded op1 runs again (got {device.op1_calls} calls)"
        )
        assert device.op2_calls == 2, (
            f"op2 re-runs with the body and succeeds the second time (got {device.op2_calls})"
        )
    finally:
        await runtime.shutdown()


class _DirectFailDevice(UniversalMockDevice):
    """A device whose action body raises directly (not via a device call), so the
    failure pauses at the ACTION level and never enters the op-level device seam."""


async def _build_non_device_fail_system() -> tuple[SystemRuntime, WorkflowTemplate, _DirectFailDevice]:
    device = _DirectFailDevice("dev1")
    transporter = create_test_transporter("robot1", ["dev1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("dev1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"dev1": device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.PAUSE)
    # No docstring on purpose: an undocumented action must describe nothing.
    async def direct_fail_action(ctx: ActionContext) -> None:
        del ctx
        raise RuntimeError("non-device action failure")

    async def _method_gen(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield direct_fail_action

    method = MethodTemplate("direct_fail_method", func=_method_gen)
    pad = system_map.get_location("pad1")

    async def _thread_gen(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        del ctx
        yield method

    thread = ThreadTemplate(labware_template=plate, start=pad, end=pad, func=_thread_gen)
    workflow = WorkflowTemplate("direct_fail_workflow")
    workflow.add_thread(thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[plate],
        resources_registry=registry, system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    runtime = SystemRuntime(builder.get_system(), event_bus=event_bus)
    return runtime, workflow, device


async def test_retry_op_rejected_when_not_op_paused() -> None:
    """RETRY_OP is valid only while a device op is suspended. A thread paused on a
    non-device action failure rejects RETRY_OP loudly, instead of the silent action
    skip the action-level path would produce for a decision it does not handle."""
    runtime, workflow, _ = await _build_non_device_fail_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        paused = await wait_for_paused_thread(runtime, record.id)
        with pytest.raises(ValueError, match="does not honour RETRY_OP"):
            runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY_OP)
        # Still recoverable by a valid decision.
        runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_THREAD)
        await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
    finally:
        await runtime.shutdown()


async def test_device_op_failed_event_fires_on_every_op_failure() -> None:
    """A device op that fails twice must emit DEVICE_OP.<cmd>.FAILED BOTH times.

    The signal is one-shot per failure, not a status: the buggy path used
    set_status, whose (command, "FAILED") dedup on the single per-runtime
    StatusManager dropped every failure after the first for a given command name.
    """
    runtime, workflow, device = await _build_two_op_system(fail_times=2)
    device_op_failed: list[str] = []

    def _capture(event_name: str, context: ExecutionContext) -> None:
        del context
        if event_name.startswith("DEVICE_OP.") and event_name.endswith(".FAILED"):
            device_op_failed.append(event_name)

    assert runtime._event_bus is not None
    runtime._event_bus.subscribe_all(_capture)
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        paused = await wait_for_paused_thread(runtime, record.id)
        assert device.op2_calls == 1
        runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY_OP)

        # Wait for the retried op to run and fail a second time before re-checking pause.
        for _ in range(250):
            if device.op2_calls == 2:
                break
            await asyncio.sleep(0.02)
        assert device.op2_calls == 2, "the retried op must fail a second time"
        paused_again = await wait_for_paused_thread(runtime, record.id)
        runtime.recover_thread(record.id, paused_again.id, RecoveryDecision.RETRY_OP)

        status = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED
        assert device.op2_calls == 3, "op re-ran after the second failure and succeeded"
    finally:
        await runtime.shutdown()

    assert device_op_failed == ["DEVICE_OP.seal.FAILED", "DEVICE_OP.seal.FAILED"], (
        "DEVICE_OP.<cmd>.FAILED must fire on every device-op failure; the set_status "
        f"dedup dropped the second one. Got: {device_op_failed}"
    )


async def test_device_lock_released_during_op_pause() -> None:
    """While a device op is paused for an operator decision, the device lock must be
    free: an operator wait can take hours, and holding the lock would block other
    sites of the same multi-site device (e.g. deck place/pick via the staging bridge).
    """
    runtime, workflow, device = await _build_two_op_system(fail_times=1)
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        paused = await wait_for_paused_thread(runtime, record.id)
        assert not device.lock.locked(), (
            "device lock must be released during the operator pause, not held for the wait"
        )
        runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY_OP)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED
    finally:
        await runtime.shutdown()


async def test_snapshot_names_the_device_call_the_thread_stopped_in() -> None:
    """The pause is inside one device call, and the snapshot says which one.

    "Retry device call" is a decision about a specific call, and an operator who
    cannot see which call it was is choosing between verbs on an error string
    alone. The name is tracked already to gate RETRY_OP; this puts it on the
    snapshot the operator reads.
    """
    runtime, workflow, _ = await _build_two_op_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        paused = await wait_for_paused_thread(runtime, record.id)

        assert paused.paused_device_command == "seal", (
            "the paused call is seal, the second op in the body; naming shake "
            "or nothing sends the operator to re-run the wrong thing"
        )
        assert paused.pause_message is not None
        assert "seal" in paused.pause_message
        assert "Simulated op2 (seal) failure" in paused.pause_message
        assert paused.pause_site == "DEVICE_OP", (
            "the site is what tells a client RETRY_OP is on the table here; "
            f"got {paused.pause_site!r}"
        )

        runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_THREAD)
        await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
    finally:
        await runtime.shutdown()


async def test_snapshot_names_no_device_call_when_the_body_itself_failed() -> None:
    """No device call is suspended here, so naming one would be a lie.

    Same condition the runtime uses to refuse RETRY_OP, so what the card offers
    and what the runtime accepts cannot drift apart.
    """
    runtime, workflow, _ = await _build_non_device_fail_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        paused = await wait_for_paused_thread(runtime, record.id)

        assert paused.paused_device_command is None
        assert paused.pause_message is not None
        assert "non-device action failure" in paused.pause_message
        assert paused.pause_site == "ACTION_BODY", (
            "an action's own Python raised, which is the one site where "
            f"ABORT_ACTION drops the action and the thread carries on; got "
            f"{paused.pause_site!r}"
        )

        runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_THREAD)
        await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
    finally:
        await runtime.shutdown()


async def test_snapshot_says_in_words_what_the_paused_action_does() -> None:
    """An action is named by its function, and a function name is not a sentence.

    `two_op_action on dev1 at pad1` is four identifiers, and the recovery verbs
    are chosen on exactly that. The author already wrote the sentence in the
    action's docstring, so the snapshot carries it instead of leaving the
    operator to guess what the thread was in the middle of.
    """
    runtime, workflow, _ = await _build_two_op_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        paused = await wait_for_paused_thread(runtime, record.id)
        assert paused.current_method is not None
        action = paused.current_method.current_action
        assert action is not None

        assert action.description == "Shake the plate, then seal it.", (
            "the paused action's own docstring is what says what it does; "
            f"got {action.description!r}"
        )

        runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_THREAD)
        await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
    finally:
        await runtime.shutdown()


async def test_snapshot_describes_nothing_when_the_action_is_undocumented() -> None:
    """An undocumented action describes nothing rather than repeating its name.

    Echoing the command back as a description would make every card look
    documented and teach an operator to skip the line that sometimes carries
    the only plain-language account of what is happening.
    """
    runtime, workflow, _ = await _build_non_device_fail_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        paused = await wait_for_paused_thread(runtime, record.id)
        assert paused.current_method is not None
        action = paused.current_method.current_action
        assert action is not None

        assert action.description is None

        runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_THREAD)
        await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(15)
async def test_incident_for_a_device_op_failure_advises_the_op_level_retry() -> None:
    """The advisory on the incident names the retry that actually applies here.

    An operator reading `orca incident get` sees one suggested verb and takes it.
    When the action failed inside a device call, the whole-action retry re-runs the
    body unreconciled and repeats the ops that already succeeded; the op-level
    retry reconciles hardware first and re-runs only the failed call. Advising the
    whole-action one here sends the operator to repeat work the instrument already
    did.
    """
    runtime, workflow, _ = await _build_two_op_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        paused = await wait_for_paused_thread(runtime, record.id)

        incidents = await runtime.incidents.list(category=IncidentCategory.ACTION_FAILED)
        assert len(incidents) == 1
        incident = incidents[0]
        assert incident.recovery_action == RecoveryAction.THREAD_RECOVER_RETRY_OP
        assert isinstance(incident.detail, ActionFailedContext)
        assert incident.detail.device_command == "seal", (
            "the incident must name the suspended call, so the advisory can be "
            "checked against the thread's own paused_device_command"
        )
        assert incident.detail.device_command == paused.paused_device_command
        assert "seal" in incident.message

        runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_THREAD)
        await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(15)
async def test_incident_for_a_body_failure_advises_the_whole_action_retry() -> None:
    """No device call is suspended, so the op-level retry would be refused.

    Same discriminator the runtime gates RETRY_OP on, so the advisory cannot
    name a verb the runtime will reject.
    """
    runtime, workflow, _ = await _build_non_device_fail_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        paused = await wait_for_paused_thread(runtime, record.id)

        incidents = await runtime.incidents.list(category=IncidentCategory.ACTION_FAILED)
        assert len(incidents) == 1
        incident = incidents[0]
        assert incident.recovery_action == RecoveryAction.THREAD_RECOVER_RETRY
        assert isinstance(incident.detail, ActionFailedContext)
        assert incident.detail.device_command is None
        assert paused.paused_device_command is None

        runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_THREAD)
        await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
    finally:
        await runtime.shutdown()
