"""Regression: a SHARED receiver fed by a per-group AUTO-SPAWNED feeder must not
close while an upstream thread that will still produce that feeder is live.

The bug (pre-fix): ``_evaluate_slot_closures`` closes a feeder-declared receiver
slot when no CURRENTLY-instantiated direct feeder thread is live. For a three-level
lineage ``producer -> mid -> sink`` where ``sink`` (SHARED_ACROSS_GROUPS + BATCHABLE)
is fed by ``mid`` and ``mid`` is itself auto-spawned per group by ``producer``, group
A's ``mid`` can terminate before group B's ``mid`` is spawned. At that instant no
``mid`` thread is live, so the direct-feeder check closes ``sink`` prematurely; the
receiver drains and group B's later ``mid`` mints a SECOND receiver.

The fix walks the ``contributes_to`` graph transitively: ``sink``'s feeders are
``{mid, producer}``, so a live group-B ``producer`` (which will still spawn its
``mid``) holds the slot open.

These tests remove the natural timing race by GATING group B's ``producer`` before
it spawns its ``mid`` while group A's full chain runs to ``mid`` terminal, so the
interleaving is driven by hand. Without the fix the shared slot closes at that
instant (asserted directly via ``slot.is_closed``) and a second receiver is spawned.
The assertion fails every time on unfixed code, never intermittently.
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.plugins import MethodTracker
from orca.resource_models.labware import PlateTemplate
from orca.resource_models.labware_state import LabwareSlot
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
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
    wire_system_map,
    create_test_device,
    create_test_transporter,
)

_TERMINAL = {"COMPLETED", "ABORTED", "STOPPED", "FAILED"}


class _SecondProducerGate:
    """Parks the SECOND ``producer`` to reach the gate until released.

    The first producer passes free and spawns its ``mid``; the second is held
    BEFORE it yields the mid-spawning method, so at the moment the first group's
    ``mid`` terminates, the second group's ``mid`` does not yet exist while its
    ``producer`` is still (parked) live.
    """

    def __init__(self) -> None:
        self.second_reached = asyncio.Event()
        self.release = asyncio.Event()
        self._count = 0

    async def wait_if_second(self) -> None:
        self._count += 1
        if self._count == 2:
            self.second_reached.set()
            await self.release.wait()


async def _build_gated_convergence_system(
    gate: _SecondProducerGate,
) -> tuple[ISystem, WorkflowTemplate, EventBus]:
    """Three-level lineage: PER_GROUP ``producer`` -> auto-spawned PER_GROUP ``mid``
    -> SHARED_ACROSS_GROUPS BATCHABLE ``sink`` receiver.

    ``producer`` contributes_to ``mid`` and spawns it via ``make_mid``; ``mid``
    contributes_to ``sink`` and feeds it via ``make_sink``; ``sink`` loops
    ``while ctx.has_more_work(): yield orca.join(...)`` then finishes. The
    producer's thread body awaits ``gate.wait_if_second()`` before yielding, so
    the second group's producer parks before it can spawn its mid.
    """
    # make_mid and make_sink each converge 2 labware inputs, and convergence
    # needs one site per input.
    producer_station = create_test_device("producer_station", site_names=["site-1", "site-2"])
    mid_station = create_test_device("mid_station", site_names=["site-1", "site-2"])
    sink_station = create_test_device("sink_station")
    transporter = create_test_transporter(
        "robot1",
        ["producer_pad", "producer_station", "mid_pad", "mid_station",
         "sink_pad", "sink_station", "waste"],
    )

    producer = PlateTemplate(
        "producer", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    mid = PlateTemplate(
        "mid", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    sink = PlateTemplate(
        "sink",
        labware_type="AGenBio_1_troughplate_190000uL_Fl",
        group_sharing=GroupSharing.SHARED_ACROSS_GROUPS,
        submission_batching=SubmissionBatching.BATCHABLE,
    )

    registry = ResourceRegistry()
    registry.add_resource(producer_station)
    registry.add_resource(mid_station)
    registry.add_resource(sink_station)
    registry.add_resource(transporter)
    producer_pool = ResourcePool("producer_station", [producer_station])
    mid_pool = ResourcePool("mid_station", [mid_station])
    sink_pool = ResourcePool("sink_station", [sink_station])
    registry.add_resource_pool(producer_pool)
    registry.add_resource_pool(mid_pool)
    registry.add_resource_pool(sink_pool)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map,
        devices={
            "producer_station": producer_station,
            "mid_station": mid_station,
            "sink_station": sink_station,
        },
        pads=["producer_pad", "mid_pad", "sink_pad", "waste"],
    )

    @orca.action(device=producer_pool, inputs=[producer, mid])
    async def make_mid(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.action(device=mid_pool, inputs=[mid, sink])
    async def make_sink(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.action(device=sink_pool, inputs=[sink])
    async def finish_sink(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=200)

    async def _producer_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield make_mid
    producer_method = MethodTemplate("producer_method", func=_producer_method)

    async def _mid_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield make_sink
    mid_method = MethodTemplate("mid_method", func=_mid_method)

    async def _sink_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield finish_sink
    sink_method = MethodTemplate("sink_method", func=_sink_method)

    producer_pad = system_map.get_location("producer_pad")
    mid_pad = system_map.get_location("mid_pad")
    sink_pad = system_map.get_location("sink_pad")
    waste = system_map.get_location("waste")

    async def _producer_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        await gate.wait_if_second()
        yield producer_method
    producer_thread = ThreadTemplate(
        labware_template=producer,
        start=producer_pad,
        end=waste,
        func=_producer_thread,
        contributes_to=["mid"],
    )

    async def _mid_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join(allows=[producer_method])
        yield mid_method
    mid_thread = ThreadTemplate(
        labware_template=mid,
        start=mid_pad,
        end=waste,
        func=_mid_thread,
        contributes_to=["sink"],
    )

    async def _sink_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        while ctx.has_more_work():
            yield orca.join(allows=[mid_method])
        yield sink_method
    sink_thread = ThreadTemplate(
        labware_template=sink,
        start=sink_pad,
        end=sink_pad,
        func=_sink_thread,
    )

    workflow = WorkflowTemplate("gated_convergence_demo")
    workflow.add_thread(producer_thread, is_start=True)
    workflow.add_thread(mid_thread)
    workflow.add_thread(sink_thread)
    workflow.register_auto_spawn(mid_thread)
    workflow.register_auto_spawn(sink_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="gated_convergence_system",
        description="",
        labwares=[producer, mid, sink],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    return builder.get_system(), workflow, event_bus


def _producer_group(gid: str) -> LabwareGroup:
    return LabwareGroup(
        id=gid,
        members=(LabwareGroupMember(thread_template_name="producer"),),
    )


async def _wait_for_boot(runtime: SystemRuntime, eid: str, timeout: float = 10.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        execution = runtime._executions[eid]
        if execution.executing_workflow is not None:
            return
        await asyncio.sleep(0.02)
    raise RuntimeError("ExecutingWorkflow never attached")


async def _wait_for_mid_terminal(
    runtime: SystemRuntime, eid: str, timeout: float = 40.0,
) -> None:
    """Wait until at least one ``mid`` thread has reached terminal."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        statuses = [
            t.status for t in runtime.list_threads(eid)
            if t.name.startswith("mid")
        ]
        if statuses and any(s in _TERMINAL for s in statuses):
            return
        await asyncio.sleep(0.05)
    raise TimeoutError("no mid thread reached terminal")


def _sink_slot(runtime: SystemRuntime, eid: str) -> LabwareSlot | None:
    wf = runtime._executions[eid].executing_workflow
    assert wf is not None
    registry = wf._labware_registry
    assert registry is not None
    return next(
        (s for s in registry.all_slots().values()
         if s.labware_template_name == "sink"),
        None,
    )


def _sink_count(tracker: MethodTracker) -> int:
    return sum(1 for n in tracker.thread_names.values() if n.startswith("sink"))


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_convergence_shared_slot_survives_feeder_gap() -> None:
    """Group A's mid terminates while group B's mid is not yet spawned; the shared
    sink slot must stay open because group B's producer (a transitive feeder) is live.
    """
    gate = _SecondProducerGate()
    system, workflow, event_bus = await _build_gated_convergence_system(gate)
    runtime = SystemRuntime(system, event_bus=event_bus)
    tracker = MethodTracker()
    runtime.register_plugin(tracker)
    await runtime.start()
    try:
        sub = await runtime.submit(
            workflow,
            groups=[_producer_group("grp-1"), _producer_group("grp-2")],
            batch_mode=BatchMode.STANDALONE,
            mode=WorkflowRunMode.PURE_SIM,
        )
        eid = sub.execution_id
        await _wait_for_boot(runtime, eid)

        await asyncio.wait_for(gate.second_reached.wait(), timeout=20.0)
        await _wait_for_mid_terminal(runtime, eid)
        for _ in range(50):
            await asyncio.sleep(0)

        slot = _sink_slot(runtime, eid)
        assert slot is not None, "sink slot should exist once the first mid minted it"
        assert not slot.is_closed, (
            "shared sink slot closed prematurely: group A's mid terminated while "
            "group B's mid was not yet spawned, but group B's producer (a transitive "
            "feeder of sink) is still live and will produce that mid"
        )

        gate.release.set()
        await execution_outcome(runtime, sub, timeout=90.0)
    finally:
        gate.release.set()
        await runtime.shutdown()

    assert _sink_count(tracker) == 1, (
        "SHARED_ACROSS_GROUPS receiver must converge on ONE sink; a premature slot "
        f"close spawned a duplicate: got {_sink_count(tracker)}"
    )
    sink_tid = next(
        tid for tid, name in tracker.thread_names.items()
        if name.startswith("sink")
    )
    contributions = sum(
        1 for m in tracker.all_completed_snapshots.get(sink_tid, [])
        if m == "mid_method"
    )
    assert contributions == 2, (
        f"shared sink should process 2 mid_method contributions; got {contributions}"
    )


def test_transitive_feeders_walks_full_lineage() -> None:
    """``transitive_feeders_for`` returns the whole upstream lineage, not just direct."""
    workflow = WorkflowTemplate("lineage")
    workflow.add_thread(_dummy_thread("producer", contributes_to=["mid"]))
    workflow.add_thread(_dummy_thread("mid", contributes_to=["sink"]))
    workflow.add_thread(_dummy_thread("sink", contributes_to=[]))
    assert workflow.transitive_feeders_for("sink") == frozenset({"mid", "producer"})
    assert workflow.transitive_feeders_for("mid") == frozenset({"producer"})
    assert workflow.transitive_feeders_for("producer") == frozenset()


def _dummy_thread(name: str, contributes_to: list[str]) -> ThreadTemplate:
    async def _fn(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        if False:
            yield  # pragma: no cover - never runs; satisfies async-generator typing
    return ThreadTemplate(
        labware_template=PlateTemplate(
            name, labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black"),
        start=f"{name}_pad",
        end=f"{name}_pad",
        func=_fn,
        contributes_to=contributes_to,
    )


def test_transitive_feeders_excludes_receiver_in_cycle() -> None:
    """A contributes_to cycle must not put the receiver in its own feeder set, and
    the walk must terminate (the test completing at all proves termination).
    """
    workflow = WorkflowTemplate("cycle")
    workflow.add_thread(_dummy_thread("X", contributes_to=["Y"]))
    workflow.add_thread(_dummy_thread("Y", contributes_to=["X"]))
    assert workflow.transitive_feeders_for("X") == frozenset({"Y"})
    assert workflow.transitive_feeders_for("Y") == frozenset({"X"})
