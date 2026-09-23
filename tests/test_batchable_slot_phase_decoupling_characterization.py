"""Behavior: an orphaned receiver loop terminates once its own workers quiesce.

A receiver served by a ``while ctx.has_more_work()`` loop terminates when its
slot closes. A slot closes from one place, ``ExecutingWorkflow``'s slot-closure
evaluation, in two ways:

- a declared ``contributes_to=[<labware name>]`` feeder set goes fully terminal
  (the feeder-driven close); or
- the receiver has NO declared feeder and no in-scope worker thread remains live
  (the worker-quiescence guarantee).

Nothing is held open waiting for a submission that may never arrive: a no-feeder
receiver closes and runs its end as soon as its own contributors go idle, for
EVERY (SubmissionBatching, BatchMode) combination. Pooling is therefore
opportunistic -- a second submission pools only while the first is still in
flight; a later one gets a fresh receiver. Holding a batch open across a gap is
a separate opt-in, not the default.

This file pins the no-second-submission cells deterministically (parametrized
over the full matrix) plus the feeder-driven close. The SECOND-submission cells
are pinned where they can be deterministic (this single-``start_pad`` orphan
harness cannot host two concurrent submissions without a start-location race):
the slot-key isolation/pooling matrix in ``test_batch_mode_keying.py``;
BATCHABLE+JOIN_EXISTING pooling + overflow in ``test_hamilton_smc_batch.py``
(N=4/5/6); STANDALONE separation in ``test_sdk_smc_adaptive.py`` and
``test_submission_lifecycle_events.py``; closure scoping in
``test_scoped_slot_closure.py``.
"""

import asyncio
from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest

import orca.orca as orca
from orca.resource_models.labware import PlateTemplate
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
from orca.runtime.execution import ExecutionPhase
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.submission import BatchMode
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import ActionTemplate, IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from tests.test_helpers import (
    execution_outcome,
    create_test_device,
    create_test_transporter,
    wire_system_map,
)
from tests.test_multi_lineage_demo import _build_multi_lineage_system, _sample_group


async def _build_orphan_system(
    batching: SubmissionBatching,
) -> tuple[ISystem, WorkflowTemplate, EventBus]:
    """A receiver with NO declared feeder: the orphan harness.

    Identical in shape to ``_build_multi_lineage_system`` except the sample
    thread does NOT declare ``contributes_to=["reservoir"]``. The sample's mix
    action still rendezvous with the reservoir's ``orca.join`` (co-labware is
    driven by the action ``inputs``, not by ``contributes_to``), so the
    reservoir gets one contribution and the sample completes. With no feeder,
    the reservoir's ``while ctx.has_more_work()`` loop relies on the
    worker-quiescence guarantee to terminate. ``batching`` sets the reservoir's
    SubmissionBatching.
    """
    # mix converges 2 labware inputs, and convergence needs one site per input.
    station = create_test_device("station", site_names=["site-1", "site-2"])
    reservoir_station = create_test_device("reservoir_station")
    transporter = create_test_transporter(
        "robot1", ["start_pad", "station", "reservoir_pad", "reservoir_station", "waste"],
    )

    sample = PlateTemplate("sample", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    reservoir = PlateTemplate(
        "reservoir",
        labware_type="AGenBio_1_troughplate_190000uL_Fl",
        group_sharing=GroupSharing.SHARED_ACROSS_GROUPS,
        submission_batching=batching,
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
        pads=["start_pad", "reservoir_pad", "waste"],
    )

    @orca.action(device=station_pool, inputs=[sample, reservoir])
    async def mix(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.action(device=reservoir_pool, inputs=[reservoir])
    async def rinse(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=200)

    async def _sample_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield mix
    sample_method = MethodTemplate("sample_method", func=_sample_method)

    async def _reservoir_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield rinse
    reservoir_method = MethodTemplate("reservoir_method", func=_reservoir_method)

    start_pad = system_map.get_location("start_pad")
    reservoir_pad = system_map.get_location("reservoir_pad")
    waste = system_map.get_location("waste")

    async def _sample_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield sample_method
    sample_thread = ThreadTemplate(
        labware_template=sample,
        start=start_pad,
        end=waste,
        func=_sample_thread,
        # No contributes_to: this is the orphan. The reservoir slot has no
        # feeder to close it; worker-quiescence must.
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

    workflow = WorkflowTemplate(f"orphan_{batching.value.lower()}_demo")
    workflow.add_thread(sample_thread, is_start=True)
    workflow.add_thread(reservoir_thread)
    workflow.register_auto_spawn(reservoir_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name=f"orphan_{batching.value.lower()}_system",
        description="",
        labwares=[sample, reservoir],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    return builder.get_system(), workflow, event_bus


def _group() -> LabwareGroup:
    return LabwareGroup(
        id=str(uuid4()),
        members=(LabwareGroupMember(thread_template_name="sample"),),
    )


def _reservoir_status(runtime: SystemRuntime, eid: str) -> str | None:
    statuses = {t.name: t.status for t in runtime.list_threads(eid)}
    return next((s for n, s in statuses.items() if n.startswith("reservoir")), None)


# --- no second submission: nothing stands open ---


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
@pytest.mark.parametrize(
    "batching", [SubmissionBatching.ISOLATED, SubmissionBatching.BATCHABLE]
)
@pytest.mark.parametrize("mode", [BatchMode.STANDALONE, BatchMode.JOIN_EXISTING])
async def test_orphan_receiver_closes_on_quiescence_without_second_submission(
    batching: SubmissionBatching, mode: BatchMode,
) -> None:
    """No feeder, no second submission: the receiver closes as soon as its own
    worker quiesces, runs its disposing end, and the execution completes -- for
    EVERY (batching, mode) cell. Nothing is held open for a submission that never
    comes. This is the invariant that a BATCHABLE receiver used to violate (it
    sat open until close_execution)."""
    system, workflow, event_bus = await _build_orphan_system(batching)
    runtime = SystemRuntime(system, event_bus=event_bus)
    await runtime.start()
    try:
        submission = await runtime.submit(
            workflow, groups=[_group()], batch_mode=mode,
            mode=WorkflowRunMode.PURE_SIM,
        )
        status = await execution_outcome(runtime, submission, timeout=90.0)
        assert status.status == "completed"
        assert _reservoir_status(runtime, submission.execution_id) == "COMPLETED"
        assert system.get_location("reservoir_pad").labware is None, (
            "the receiver's disposing end must run when it closes on quiescence, "
            "clearing its end location")
    finally:
        await runtime.shutdown()


# --- feeder-declared receiver: closes via its feeder, unchanged ---


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_feeder_declared_receiver_terminates_via_feeder() -> None:
    """A receiver WITH a declared feeder terminates when the feeder completes,
    while still ACCEPTING, with ``close_execution`` never called -- the
    feeder-driven close, unchanged by the quiescence guarantee.
    """
    system, workflow, event_bus = await _build_multi_lineage_system()
    runtime = SystemRuntime(system, event_bus=event_bus)
    await runtime.start()
    try:
        submission = await runtime.submit(
            workflow, groups=[_sample_group()], mode=WorkflowRunMode.PURE_SIM,
        )
        execution = runtime._executions[submission.execution_id]
        assert execution.phase is ExecutionPhase.ACCEPTING
        status = await execution_outcome(runtime, submission, timeout=60.0)
        assert status.status == "completed"
        assert execution.phase is ExecutionPhase.COMPLETED
    finally:
        await runtime.shutdown()
