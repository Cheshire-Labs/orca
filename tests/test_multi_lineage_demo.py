"""T6h: abstract multi-lineage demo.

Validates the T6 primitives composed together in a workflow that is NOT
SMC-specific:
- A PER_GROUP ``sample`` plate that each group owns.
- A SHARED_ACROSS_GROUPS ``reservoir`` that is one physical plate for every
  group within a submission (the group-aware slot key collapses the group
  component).

With N=1 and N=3 groups, the same workflow code should produce:
- N sample threads (one per group) with distinct group_ids.
- Exactly 1 reservoir thread (the SHARED receiver), visited N times.

The single-pad sim topology serializes sample pickups through ``start_pad``
via the initialize-labware retry loop; that's expected. The assertion is
about the *shape* of what ran (how many threads, how many joins), not
wall-clock parallelism.
"""

from orca.config import OrcaConfig
import asyncio
from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest

import orca.orca as orca
from tests.thread_recorder import ThreadRecorder
from orca.resource_models.labware import PlateTemplate
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
from orca.workflow_models.labware_group import LabwareGroup, LabwareGroupMember
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.modes.run_modes import WorkflowRunMode
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from tests.test_helpers import (
    execution_outcome,
    wire_system_map,
    create_test_device,
    create_test_transporter,
)
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.memory_activity import memory_unsaved_positions
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service


async def _build_multi_lineage_system(
    extra_pads: tuple[str, ...] = (),
    mix_gate: asyncio.Event | None = None,
) -> tuple[ISystem, WorkflowTemplate, EventBus]:
    """Two-template workflow: PER_GROUP sample + SHARED_ACROSS_GROUPS reservoir.

    Sample declares contributes_to=["reservoir"] so the reservoir's slot
    closes after every sample has exhausted its contributions. The reservoir
    receiver uses ``while ctx.has_more_work(): yield orca.join(...)`` so it
    accepts N mix contributions and exits after the close signal.
    """
    station = create_test_device("station", site_names=["site-1", "site-2"])
    reservoir_station = create_test_device("reservoir_station")
    transporter = create_test_transporter(
        "robot1",
        ["start_pad", "station", "reservoir_pad", "reservoir_station", "waste", *extra_pads],
    )

    sample = PlateTemplate("sample", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    reservoir = PlateTemplate(
        "reservoir",
        labware_type="AGenBio_1_troughplate_190000uL_Fl",
        group_sharing=GroupSharing.SHARED_ACROSS_GROUPS,
        submission_batching=SubmissionBatching.BATCHABLE,
    )

    registry = ResourceRegistry()
    registry.add_resource(station)
    registry.add_resource(reservoir_station)
    registry.add_resource(transporter)
    station_pool = ResourcePool("station", [station])
    reservoir_pool = ResourcePool("reservoir_station", [reservoir_station])
    registry.add_resource_pool(station_pool)
    registry.add_resource_pool(reservoir_pool)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map,
        devices={"station": station, "reservoir_station": reservoir_station},
        pads=["start_pad", "reservoir_pad", "waste", *extra_pads],
    )

    @orca.action(device=station_pool, inputs=[sample, reservoir])
    async def mix(ctx: object) -> None:
        if mix_gate is not None:
            await mix_gate.wait()
        await ctx.device().shake(duration=1, speed=500)

    @orca.action(device=reservoir_pool, inputs=[reservoir])
    async def rinse(ctx: object) -> None:
        await ctx.device().shake(duration=1, speed=200)

    async def _sample_method(ctx: object) -> AsyncGenerator[object, None]:
        yield mix
    sample_method = MethodTemplate("sample_method", func=_sample_method)

    async def _reservoir_method(ctx: object) -> AsyncGenerator[object, None]:
        yield rinse
    reservoir_method = MethodTemplate("reservoir_method", func=_reservoir_method)

    start_pad = system_map.get_location("start_pad")
    reservoir_pad = system_map.get_location("reservoir_pad")
    waste = system_map.get_location("waste")

    async def _sample_thread(ctx: object) -> AsyncGenerator[MethodTemplate, None]:
        yield sample_method
    sample_thread = ThreadTemplate(
        labware_template=sample,
        start=start_pad,
        end=waste,
        func=_sample_thread,
        contributes_to=["reservoir"],
    )

    async def _reservoir_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        while ctx.has_more_work():
            yield orca.join(allows=[sample_method])
            if ctx.has_more_work():
                yield orca.park("reservoir_pad")
        yield reservoir_method
    reservoir_thread = ThreadTemplate(
        labware_template=reservoir,
        start=reservoir_pad,
        end=reservoir_pad,
        func=_reservoir_thread,
    )

    workflow = WorkflowTemplate("multi_lineage_demo")
    workflow.add_thread(sample_thread, is_start=True)
    workflow.add_thread(reservoir_thread)
    workflow.register_auto_spawn(reservoir_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="multi_lineage_system",
        description="",
        labwares=[sample, reservoir],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus, config=OrcaConfig(), ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(),
        unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    return builder.get_system(), workflow, event_bus


def _sample_group() -> LabwareGroup:
    return LabwareGroup(
        id=str(uuid4()),
        members=(LabwareGroupMember(thread_template_name="sample"),),
    )


class TestMultiLineage:

    @pytest.mark.slow
    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_multi_lineage_n1(self) -> None:
        """Baseline: N=1 group produces 1 sample thread and 1 reservoir thread."""
        system, workflow, event_bus = await _build_multi_lineage_system()
        runtime = await memory_runtime(system, event_bus=event_bus)
        recorder = ThreadRecorder()
        runtime.register_plugin(recorder)
        await runtime.start()

        submission = await runtime.submit(workflow, groups=[_sample_group()], mode=WorkflowRunMode.CHECK_RUN)
        status = await execution_outcome(runtime, submission, timeout=60.0)
        await runtime.shutdown()
        assert status.status == "completed"

        sample_count = sum(
            1 for name in recorder.thread_names.values()
            if name.startswith("sample")
        )
        reservoir_count = sum(
            1 for name in recorder.thread_names.values()
            if name.startswith("reservoir")
        )
        assert sample_count == 1, f"Expected 1 sample thread; got {sample_count}"
        assert reservoir_count == 1, f"Expected 1 reservoir thread; got {reservoir_count}"

    @pytest.mark.slow
    @pytest.mark.asyncio
    @pytest.mark.timeout(600)
    async def test_multi_lineage_n3(self) -> None:
        """N=3 groups produce 3 sample threads and 1 SHARED reservoir thread.

        Validates SHARED_ACROSS_GROUPS slot-key collapse: all three samples
        route their mix contributions to the same reservoir slot. The
        reservoir receiver joins N times and closes when all samples finish.
        """
        system, workflow, event_bus = await _build_multi_lineage_system()
        runtime = await memory_runtime(system, event_bus=event_bus)
        recorder = ThreadRecorder()
        runtime.register_plugin(recorder)
        await runtime.start()

        groups = [_sample_group() for _ in range(3)]
        submission = await runtime.submit(workflow, groups=groups, mode=WorkflowRunMode.CHECK_RUN)
        status = await execution_outcome(runtime, submission, timeout=540.0)
        await runtime.shutdown()
        assert status.status == "completed"

        sample_count = sum(
            1 for name in recorder.thread_names.values()
            if name.startswith("sample")
        )
        reservoir_count = sum(
            1 for name in recorder.thread_names.values()
            if name.startswith("reservoir")
        )
        assert sample_count == 3, f"Expected 3 sample threads; got {sample_count}"
        assert reservoir_count == 1, (
            f"SHARED_ACROSS_GROUPS should collapse to ONE reservoir thread; "
            f"got {reservoir_count}"
        )

        reservoir_tid = next(
            tid for tid, name in recorder.thread_names.items()
            if name.startswith("reservoir")
        )
        reservoir_methods = recorder.completed_methods.get(reservoir_tid, [])
        # The recorder keeps method names; each sample contribution runs
        # sample_method (which wraps the mix action). Counting sample_method
        # occurrences is equivalent to counting mix contributions here.
        mix_count = sum(1 for m in reservoir_methods if m == "sample_method")
        assert mix_count >= 3, (
            f"Shared reservoir should process at least 3 mix contributions; "
            f"got {mix_count}: {reservoir_methods}"
        )
