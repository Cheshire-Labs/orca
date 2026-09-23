"""T6h coverage validation: @orca.thread(required=False) + submit-time
group-coverage enforcement.

- Required PER_GROUP entry threads: every submitted group must carry a
  LabwareGroupMember whose thread_template_name matches. Else reject.
- Required SHARED_ACROSS_GROUPS entry threads: at least one group must
  carry a member. Else reject.
- Optional threads (required=False): groups may omit the member.
- Member thread_template_name must be known (entry OR auto-spawn
  registered). Unknown names reject.
"""
from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest

import orca.orca as orca
from orca.resource_models.labware import PlateTemplate
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
from orca.runtime.labware_group import (
    AcquisitionValidationError,
    LabwareGroup,
    LabwareGroupMember,
)
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.runtime.run_modes import WorkflowRunMode
from tests.test_helpers import create_test_device, create_test_transporter, wire_system_map


async def _build_required_opt_system(*, sample_required: bool, reservoir_required: bool):
    """Two entry templates (sample PER_GROUP, reservoir SHARED_ACROSS_GROUPS),
    each parameterized required/optional for this test."""
    station = create_test_device("station")
    transporter = create_test_transporter("robot1", ["start_pad", "station", "waste"])

    sample = PlateTemplate("sample", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    reservoir = PlateTemplate(
        "reservoir",
        labware_type="AGenBio_1_troughplate_190000uL_Fl",
        group_sharing=GroupSharing.SHARED_ACROSS_GROUPS,
        submission_batching=SubmissionBatching.BATCHABLE,
    )

    registry = ResourceRegistry()
    registry.add_resource(station)
    registry.add_resource(transporter)
    station_pool = ResourcePool("station", [station])
    registry.add_resource_pool(station_pool)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map, devices={"station": station}, pads=["start_pad", "waste"],
    )

    @orca.action(device=station_pool, inputs=[sample])
    async def mix(ctx: object) -> None:
        await ctx.device().shake(duration=1, speed=500)

    async def _mix_method(ctx: object) -> AsyncGenerator[object, None]:
        yield mix
    sample_method = MethodTemplate("sample_method", func=_mix_method)

    async def _sample_thread(ctx: object) -> AsyncGenerator[MethodTemplate, None]:
        yield sample_method

    async def _reservoir_thread(ctx: object) -> AsyncGenerator[MethodTemplate, None]:
        yield sample_method

    start_pad = system_map.get_location("start_pad")
    waste = system_map.get_location("waste")

    sample_template = ThreadTemplate(
        labware_template=sample, start=start_pad, end=waste,
        func=_sample_thread, required=sample_required,
    )
    reservoir_template = ThreadTemplate(
        labware_template=reservoir, start=start_pad, end=waste,
        func=_reservoir_thread, required=reservoir_required,
    )

    workflow = WorkflowTemplate("required_test_wf")
    workflow.add_thread(sample_template, is_start=True)
    workflow.add_thread(reservoir_template, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="required_test",
        description="",
        labwares=[sample, reservoir],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    return builder.get_system(), workflow, event_bus


class TestRequiredCoverage:

    @pytest.mark.asyncio
    async def test_rejects_group_missing_required_per_group_member(self) -> None:
        system, workflow, event_bus = await _build_required_opt_system(
            sample_required=True, reservoir_required=False,
        )
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            group = LabwareGroup(id=str(uuid4()), members=())
            with pytest.raises(AcquisitionValidationError, match="sample"):
                await runtime.submit(workflow, groups=[group], mode=WorkflowRunMode.PURE_SIM)
        finally:
            await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_rejects_submission_missing_required_shared_member(self) -> None:
        system, workflow, event_bus = await _build_required_opt_system(
            sample_required=False, reservoir_required=True,
        )
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            group = LabwareGroup(
                id=str(uuid4()),
                members=(LabwareGroupMember(thread_template_name="sample"),),
            )
            with pytest.raises(AcquisitionValidationError, match="reservoir"):
                await runtime.submit(workflow, groups=[group], mode=WorkflowRunMode.PURE_SIM)
        finally:
            await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_accepts_group_missing_optional_member(self) -> None:
        system, workflow, event_bus = await _build_required_opt_system(
            sample_required=True, reservoir_required=False,
        )
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            group = LabwareGroup(
                id=str(uuid4()),
                members=(LabwareGroupMember(thread_template_name="sample"),),
            )
            # Should not raise — reservoir is optional.
            submission = await runtime.submit(workflow, groups=[group], mode=WorkflowRunMode.PURE_SIM)
            assert submission.id is not None
        finally:
            await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_rejects_member_with_unknown_thread_name(self) -> None:
        system, workflow, event_bus = await _build_required_opt_system(
            sample_required=False, reservoir_required=False,
        )
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            group = LabwareGroup(
                id=str(uuid4()),
                members=(LabwareGroupMember(thread_template_name="ghost_thread"),),
            )
            with pytest.raises(AcquisitionValidationError, match="ghost_thread"):
                await runtime.submit(workflow, groups=[group], mode=WorkflowRunMode.PURE_SIM)
        finally:
            await runtime.shutdown()
