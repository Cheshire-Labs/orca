"""An action that needs the plate of a `required=False` thread the group left out pauses at once.

Plate `sample`'s combine needs plate `control`, whose thread is optional. A group that leaves the control
out means no control plate ever comes, so the sample pauses at CO_LABWARE_LOST naming the plate and the
action. Only ABORT_ACTION and ABORT_THREAD are offered: there is nothing to retry. When the method's `if`
skips the combine, the run completes without the control.
"""

import asyncio
from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest

import orca.orca as orca
from orca.config import OrcaConfig
from orca.events.event_bus import EventBus
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.status_models import ThreadSnapshot
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.labware_group import LabwareGroup, LabwareGroupMember
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.status_enums import PauseSite, RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflows.workflow_factories import _left_out_by
from orca.workflow_models.thread_template import ThreadTemplate
from orca.resource_models.labware import PlateTemplate
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service, memory_unsaved_positions
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.mock import UniversalMockDevice
from tests.test_helpers import create_test_plate_template, create_test_transporter, wait_for_runtime_condition, wire_system_map


class _CountsShakes(UniversalMockDevice):
    def __init__(self, name: str, site_names: list[str] | None = None) -> None:
        super().__init__(name, site_names=site_names)
        self.speeds: list[int] = []

    async def shake(self, duration: int, speed: int) -> None:
        self.speeds.append(speed)
        await super().shake(duration, speed)


async def _a_sample_whose_combine_needs_the_optional_control(
    *, combines: bool,
) -> tuple[SystemRuntime, str, _CountsShakes]:
    combiner = _CountsShakes("combiner", site_names=["site-1", "site-2"])
    transporter = create_test_transporter("robot1", ["combiner", "pad1", "pad2"])
    sample = create_test_plate_template("sample")
    control = create_test_plate_template("control")
    registry = ResourceRegistry()
    registry.add_resource(combiner)
    registry.add_resource(transporter)
    pool = ResourcePool("combiner", [combiner])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"combiner": combiner}, pads=["pad1", "pad2"])

    @orca.action(device=pool, inputs=[sample, control])
    async def combine(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=100)

    @orca.action(device=pool, inputs=[sample])
    async def shake_sample(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=200)

    @orca.method
    async def sample_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        if combines:
            yield combine
        yield shake_sample

    pad1 = system_map.get_location("pad1")
    pad2 = system_map.get_location("pad2")

    @orca.thread(labware=sample, start=pad1, end=pad1)
    async def sample_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield sample_method

    @orca.thread(labware=control, start=pad2, end=pad2, required=False)
    async def control_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join(allows=[sample_method])

    @orca.workflow(name="optional_control_input")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(sample_journey)
        wf.start(control_journey)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="optional_input", description="", labwares=[sample, control], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=event_bus, config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    runtime = await memory_runtime(builder.get_system(), event_bus=event_bus)
    await runtime.start()
    return runtime, await _submit_leaving_the_control_out(runtime, workflow), combiner


async def _submit_leaving_the_control_out(runtime: SystemRuntime, workflow: WorkflowTemplate) -> str:
    group = LabwareGroup(id=str(uuid4()), members=(LabwareGroupMember("sample_journey"),))
    submission = await runtime.submit(workflow, groups=[group], mode=WorkflowRunMode.CHECK_RUN)
    return submission.execution_id


def _sample(runtime: SystemRuntime, execution_id: str) -> ThreadSnapshot:
    [found] = [t for t in runtime.get_execution_detail(execution_id).threads if t.name.startswith("sample")]
    return found


def _sample_is_error_paused(runtime: SystemRuntime, execution_id: str) -> bool:
    threads = [t for t in runtime.get_execution_detail(execution_id).threads if t.name.startswith("sample")]
    return bool(threads) and threads[0].pause_reason == "error"


async def _until_the_sample_pauses(runtime: SystemRuntime, execution_id: str) -> ThreadSnapshot:
    await wait_for_runtime_condition(
        runtime, lambda: _sample_is_error_paused(runtime, execution_id),
        timeout=10.0, message="the sample to pause for the left-out control",
    )
    return _sample(runtime, execution_id)


async def test_the_sample_pauses_naming_the_left_out_plate_and_the_action() -> None:
    runtime, execution_id, combiner = await _a_sample_whose_combine_needs_the_optional_control(combines=True)
    try:
        sample = await _until_the_sample_pauses(runtime, execution_id)

        assert sample.pause_site == PauseSite.CO_LABWARE_LOST.value, sample
        assert sample.honoured_decisions == ("ABORT_ACTION", "ABORT_THREAD"), sample
        message = sample.last_error or ""
        assert "'control'" in message and "'combine'" in message, message
        assert "control_journey" in message and "wf.thread" in message, message
        assert combiner.speeds == []
    finally:
        await runtime.shutdown()


async def test_abort_action_skips_the_combine_and_the_sample_carries_on() -> None:
    runtime, execution_id, combiner = await _a_sample_whose_combine_needs_the_optional_control(combines=True)
    try:
        sample = await _until_the_sample_pauses(runtime, execution_id)
        runtime.recover_thread(execution_id, sample.id, RecoveryDecision.ABORT_ACTION)
        await asyncio.wait_for(runtime.wait(execution_id), timeout=10.0)

        assert combiner.speeds == [200]
        assert _sample(runtime, execution_id).status == "COMPLETED"
    finally:
        await runtime.shutdown()


async def test_abort_thread_ends_the_sample() -> None:
    runtime, execution_id, combiner = await _a_sample_whose_combine_needs_the_optional_control(combines=True)
    try:
        sample = await _until_the_sample_pauses(runtime, execution_id)
        runtime.recover_thread(execution_id, sample.id, RecoveryDecision.ABORT_THREAD)
        await asyncio.wait_for(runtime.wait(execution_id), timeout=10.0)

        assert combiner.speeds == []
        assert _sample(runtime, execution_id).status == "ABORTED"
    finally:
        await runtime.shutdown()


async def test_retry_is_refused_because_the_plate_will_never_come() -> None:
    runtime, execution_id, _ = await _a_sample_whose_combine_needs_the_optional_control(combines=True)
    try:
        sample = await _until_the_sample_pauses(runtime, execution_id)

        with pytest.raises(ValueError, match="does not honour"):
            runtime.recover_thread(execution_id, sample.id, RecoveryDecision.RETRY)
        assert _sample(runtime, execution_id).pause_reason == "error"
    finally:
        await runtime.shutdown()


async def test_a_method_whose_if_skips_the_combine_completes_without_the_control() -> None:
    runtime, execution_id, combiner = await _a_sample_whose_combine_needs_the_optional_control(combines=False)
    try:
        final = await asyncio.wait_for(runtime.wait(execution_id), timeout=10.0)

        assert final.status is ExecutionState.COMPLETED
        assert combiner.speeds == [200]
        assert [t.name.split("-")[0] for t in runtime.list_threads(execution_id)] == ["sample"]
    finally:
        await runtime.shutdown()


async def _a_spawned_helper_whose_combine_needs_the_optional_control() -> tuple[SystemRuntime, str]:
    device = _CountsShakes("combiner", site_names=["site-1", "site-2"])
    transporter = create_test_transporter("robot1", ["combiner", "pad1", "pad2", "pad3"])
    sample = create_test_plate_template("sample")
    helper = create_test_plate_template("helper")
    control = create_test_plate_template("control")
    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("combiner", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"combiner": device}, pads=["pad1", "pad2", "pad3"])

    @orca.action(device=pool, inputs=[sample, helper])
    async def prepare(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=100)

    @orca.action(device=pool, inputs=[helper, control])
    async def combine(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=200)

    @orca.method
    async def sample_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield prepare

    @orca.method
    async def helper_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield combine

    pad1, pad2, pad3 = (system_map.get_location(name) for name in ("pad1", "pad2", "pad3"))

    @orca.thread(labware=sample, start=pad1, end=pad1)
    async def sample_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield sample_method

    @orca.thread(labware=helper, start=pad2, end=pad2)
    async def helper_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join(allows=[sample_method])
        yield helper_method

    @orca.thread(labware=control, start=pad3, end=pad3, required=False)
    async def control_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join(allows=[helper_method])

    @orca.workflow(name="spawned_helper_needs_the_control")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(sample_journey)
        wf.thread(helper_journey)
        wf.start(control_journey)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="spawned_helper", description="", labwares=[sample, helper, control], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=event_bus, config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    runtime = await memory_runtime(builder.get_system(), event_bus=event_bus)
    await runtime.start()
    return runtime, await _submit_leaving_the_control_out(runtime, workflow)


async def test_a_thread_spawned_for_the_group_pauses_when_its_action_needs_the_left_out_plate() -> None:
    runtime, execution_id = await _a_spawned_helper_whose_combine_needs_the_optional_control()
    try:
        def helper() -> list[ThreadSnapshot]:
            return [t for t in runtime.get_execution_detail(execution_id).threads if t.name.startswith("helper")]

        await wait_for_runtime_condition(
            runtime, lambda: bool(helper()) and helper()[0].pause_reason == "error",
            timeout=10.0, message="the spawned helper to pause for the left-out control",
        )
        [paused] = helper()

        assert paused.pause_site == PauseSite.CO_LABWARE_LOST.value, paused
        assert "'control'" in (paused.last_error or "") and "'combine'" in (paused.last_error or ""), paused
    finally:
        await runtime.shutdown()


def _optional_control(
    sharing: GroupSharing = GroupSharing.PER_GROUP, batching: SubmissionBatching = SubmissionBatching.ISOLATED,
) -> tuple[WorkflowTemplate, ThreadTemplate]:
    control = PlateTemplate(
        "control", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black",
        group_sharing=sharing, submission_batching=batching,
    )
    thread = ThreadTemplate(labware_template=control, start="pad2", end="pad2", func=_control_journey, required=False)
    workflow = WorkflowTemplate("left_out_rules")
    workflow.add_thread(thread, is_start=True)
    return workflow, thread


async def _control_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
    return
    yield


def test_an_optional_thread_no_one_names_is_left_out() -> None:
    workflow, _ = _optional_control()

    assert _left_out_by({}, [{}], workflow) == {"control": "_control_journey"}


def test_an_optional_thread_the_group_names_is_not_left_out() -> None:
    workflow, _ = _optional_control()

    assert _left_out_by({"control": "control"}, [{"control": "control"}], workflow) == {}


def test_a_shared_plate_another_group_names_is_not_left_out() -> None:
    workflow, _ = _optional_control(sharing=GroupSharing.SHARED_ACROSS_GROUPS)

    assert _left_out_by({}, [{}, {"control": "control"}], workflow) == {}
    assert _left_out_by({}, [{}, {}], workflow) == {"control": "_control_journey"}


def test_a_batchable_plate_is_not_left_out_because_a_later_submission_can_bring_it() -> None:
    workflow, _ = _optional_control(batching=SubmissionBatching.BATCHABLE)

    assert _left_out_by({}, [{}], workflow) == {}


def test_a_plate_whose_thread_is_also_declared_with_wf_thread_is_not_left_out() -> None:
    workflow, thread = _optional_control()
    workflow.register_auto_spawn(thread)

    assert _left_out_by({}, [{}], workflow) == {}
