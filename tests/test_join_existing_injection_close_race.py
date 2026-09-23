"""Regression: a JOIN_EXISTING submission injected while an existing feeder is
terminating must still converge on the single shared receiver.

The bug (pre-fix): ``_inject_submission`` registers the joining submission's feeder
threads as live only AFTER awaiting (acquisition resolution, then
``build_entry_threads_for``). If an existing feeder of the target execution
terminates during that window, ``_evaluate_slot_closures`` sees no live feeder for
the shared receiver's slot and closes it prematurely; the receiver drains and the
late feeder mints a SECOND receiver.

These tests remove the natural ~20% timing race by GATING both sides so the
interleaving is driven by hand and the outcome is deterministic:
- The feeder's first ``mix`` parks on a test event, so the existing feeder cannot
  terminate until we release it (and only after the joining submission is parked
  mid-injection).
- The joining submission parks inside its injection window (either the
  ``build_entry_threads_for`` await = W1, or the ``_resolve_acquisitions`` await =
  W2), so the feeder terminal lands squarely inside the vulnerable window.

Without the fix the shared slot closes during the window (asserted directly via
``slot.is_closed``) and a second receiver is spawned. With the fix the close is
held until the joining feeder is live, so exactly one receiver results. The
``is_closed`` assertion fails every time on unfixed code, never intermittently.
"""

import asyncio
import traceback
from collections.abc import AsyncGenerator, Sequence
from uuid import uuid4

import pytest

import orca.orca as orca
from orca.plugins import MethodTracker
from orca.resource_models.labware import PlateTemplate
from orca.resource_models.labware_state import LabwareSlot
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.submission import BatchMode, ResolvedAcquisition
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
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


class _FeederGate:
    """Parks the FIRST feeder ``mix`` until released; later feeders run free.

    The joining submission's feeder never exists until we release the injection
    window, so the first ``mix`` is deterministically the existing submission's.
    """

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self._gated_one = False

    async def wait_first(self) -> None:
        if not self._gated_one:
            self._gated_one = True
            self.reached.set()
            await self.release.wait()


async def _build_gated_lineage_system(
    gate: _FeederGate,
) -> tuple[ISystem, WorkflowTemplate, EventBus]:
    """PER_GROUP ``sample`` feeder + SHARED_ACROSS_GROUPS BATCHABLE ``reservoir``
    receiver, with the feeder's ``mix`` gated on ``gate`` for its first call.

    Shape mirrors ``tests.test_multi_lineage_demo._build_multi_lineage_system``;
    the only addition is the gate so the existing feeder can be held just before
    it terminates.
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
        pads=["start_pad", "reservoir_pad", "waste"],
    )

    @orca.action(device=station_pool, inputs=[sample, reservoir])
    async def mix(ctx: ActionContext) -> None:
        await gate.wait_first()
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

    async def _sample_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
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

    workflow = WorkflowTemplate("gated_lineage_demo")
    workflow.add_thread(sample_thread, is_start=True)
    workflow.add_thread(reservoir_thread)
    workflow.register_auto_spawn(reservoir_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="gated_lineage_system",
        description="",
        labwares=[sample, reservoir],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    return builder.get_system(), workflow, event_bus


def _sample_group() -> LabwareGroup:
    return LabwareGroup(
        id=str(uuid4()),
        members=(LabwareGroupMember(thread_template_name="sample"),),
    )


async def _wait_for_boot(runtime: SystemRuntime, eid: str, timeout: float = 10.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        execution = runtime._executions[eid]
        if execution.executing_workflow is not None:
            return
        await asyncio.sleep(0.02)
    raise RuntimeError("ExecutingWorkflow never attached")


async def _wait_for_sample_terminal(
    runtime: SystemRuntime, eid: str, timeout: float = 30.0,
) -> None:
    statuses: dict[str, str] = {}
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        statuses = {t.name: t.status for t in runtime.list_threads(eid)}
        sample_states = [s for n, s in statuses.items() if n.startswith("sample")]
        if sample_states and all(s in _TERMINAL for s in sample_states):
            return
        await asyncio.sleep(0.05)
    # One observed hang here was a lost task wakeup with a fully quiescent
    # engine; the task stacks are the only evidence that can pin the parked one.
    stacks = "\n".join(
        f"--- {task.get_name()}\n"
        + "".join(traceback.format_stack(frame, limit=3))
        for task in asyncio.all_tasks()
        for frame in task.get_stack(limit=3)[-1:]
    )
    raise TimeoutError(
        f"sample thread never reached terminal; threads: {statuses}\n"
        f"pending task stacks:\n{stacks}"
    )


def _reservoir_slot(runtime: SystemRuntime, eid: str) -> LabwareSlot | None:
    wf = runtime._executions[eid].executing_workflow
    assert wf is not None
    registry = wf._labware_registry
    assert registry is not None
    return next(
        (s for s in registry.all_slots().values()
         if s.labware_template_name == "reservoir"),
        None,
    )


def _reservoir_count(tracker: MethodTracker) -> int:
    return sum(1 for n in tracker.thread_names.values() if n.startswith("reservoir"))


async def _drive_race(
    runtime: SystemRuntime,
    workflow: WorkflowTemplate,
    gate: _FeederGate,
    tracker: MethodTracker,
    park_window: str,
) -> None:
    """Drive the deterministic interleaving.

    ``park_window`` selects which injection await the joining submission parks on:
    ``"build"`` (W1, ``build_entry_threads_for``) or ``"resolve"`` (W2,
    ``_resolve_acquisitions``). In both cases the existing feeder is driven to
    terminal while the join is parked, then the shared slot is asserted still open,
    then the window is released and exactly one receiver is asserted.
    """
    window_entered = asyncio.Event()
    release_window = asyncio.Event()

    if park_window == "build":
        original_build = runtime._system.build_entry_threads_for

        async def gated_build(
            template: WorkflowTemplate,
            submission_id: str,
            groups: Sequence[LabwareGroup],
            batch_mode: BatchMode = BatchMode.STANDALONE,
            resolved_acquisitions: dict[tuple[str, str], ResolvedAcquisition] | None = None,
            *,
            run_mode: WorkflowRunMode,
        ) -> list[LabwareThreadInstance]:
            window_entered.set()
            await release_window.wait()
            return await original_build(
                template, submission_id, groups, batch_mode,
                resolved_acquisitions, run_mode=run_mode,
            )

        runtime._system.build_entry_threads_for = gated_build  # type: ignore[method-assign]
    else:
        original_resolve = runtime._resolve_acquisitions
        # Every submit calls _resolve_acquisitions; gate only the SECOND call
        # (the joining submission's) so the first submission still boots.
        resolve_calls = {"n": 0}

        async def gated_resolve(
            groups: Sequence[LabwareGroup],
        ) -> dict[tuple[str, str], ResolvedAcquisition]:
            resolve_calls["n"] += 1
            if resolve_calls["n"] >= 2:
                window_entered.set()
                await release_window.wait()
            return await original_resolve(groups)

        runtime._resolve_acquisitions = gated_resolve  # type: ignore[method-assign]

    sub1 = await runtime.submit(
        workflow, groups=[_sample_group()],
        batch_mode=BatchMode.JOIN_EXISTING, mode=WorkflowRunMode.PURE_SIM,
    )
    eid = sub1.execution_id
    await _wait_for_boot(runtime, eid)
    await asyncio.wait_for(gate.reached.wait(), timeout=15.0)

    sub2_task = asyncio.create_task(runtime.submit(
        workflow, groups=[_sample_group()],
        batch_mode=BatchMode.JOIN_EXISTING, mode=WorkflowRunMode.PURE_SIM,
    ))
    try:
        await asyncio.wait_for(window_entered.wait(), timeout=15.0)

        gate.release.set()
        await _wait_for_sample_terminal(runtime, eid)
        for _ in range(50):
            await asyncio.sleep(0)

        slot = _reservoir_slot(runtime, eid)
        assert slot is not None, "reservoir slot should exist once the feeder minted it"
        assert not slot.is_closed, (
            "reservoir slot closed prematurely during JOIN_EXISTING injection: "
            "an existing feeder terminated inside the injection window before the "
            "joining feeder was registered live"
        )

        release_window.set()
        sub2 = await asyncio.wait_for(sub2_task, timeout=15.0)
        assert sub1.execution_id == sub2.execution_id
        await execution_outcome(runtime, sub1, timeout=60.0)
    finally:
        release_window.set()
        gate.release.set()
        if not sub2_task.done():
            sub2_task.cancel()

    assert _reservoir_count(tracker) == 1, (
        "JOIN_EXISTING must converge on ONE shared receiver; a premature slot "
        f"close spawned a duplicate: got {_reservoir_count(tracker)}"
    )


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_join_existing_survives_feeder_terminal_in_build_window() -> None:
    """W1: feeder terminates while the join is parked in ``build_entry_threads_for``."""
    gate = _FeederGate()
    system, workflow, event_bus = await _build_gated_lineage_system(gate)
    runtime = SystemRuntime(system, event_bus=event_bus)
    tracker = MethodTracker()
    runtime.register_plugin(tracker)
    await runtime.start()
    try:
        await _drive_race(runtime, workflow, gate, tracker, park_window="build")
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_join_existing_survives_feeder_terminal_in_resolve_window() -> None:
    """W2: feeder terminates while the join is parked in ``_resolve_acquisitions``.

    This window precedes the join decision on unfixed code, so a guard placed only
    around ``build_entry_threads_for`` would miss it. The test pins that the fix
    covers the whole accept->resolve->build->register window (the race CLASS), not
    just the build instance.
    """
    gate = _FeederGate()
    system, workflow, event_bus = await _build_gated_lineage_system(gate)
    runtime = SystemRuntime(system, event_bus=event_bus)
    tracker = MethodTracker()
    runtime.register_plugin(tracker)
    await runtime.start()
    try:
        await _drive_race(runtime, workflow, gate, tracker, park_window="resolve")
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_injecting_guard_holds_then_releases_slot_close() -> None:
    """Contract of the guard itself, via the real ``injecting()`` context manager.

    While inside ``injecting()`` a terminating feeder must NOT close the shared
    slot; on exit the deferred close must fire (the re-evaluation that keeps the
    guard hang-safe). No second submission is involved: this isolates the guard's
    effect on ``_evaluate_slot_closures`` and its exit re-eval.
    """
    gate = _FeederGate()
    system, workflow, event_bus = await _build_gated_lineage_system(gate)
    runtime = SystemRuntime(system, event_bus=event_bus)
    await runtime.start()
    try:
        sub = await runtime.submit(
            workflow, groups=[_sample_group()],
            batch_mode=BatchMode.JOIN_EXISTING, mode=WorkflowRunMode.PURE_SIM,
        )
        eid = sub.execution_id
        await _wait_for_boot(runtime, eid)
        await asyncio.wait_for(gate.reached.wait(), timeout=15.0)

        wf = runtime._executions[eid].executing_workflow
        assert wf is not None

        async with wf.injecting():
            gate.release.set()
            await _wait_for_sample_terminal(runtime, eid)
            for _ in range(50):
                await asyncio.sleep(0)
            slot = _reservoir_slot(runtime, eid)
            assert slot is not None
            assert not slot.is_closed, (
                "the shared slot must stay open while an injection is pending, "
                "even though its only feeder has terminated"
            )

        for _ in range(10):
            await asyncio.sleep(0)
        assert slot.is_closed, (
            "leaving injecting() must re-evaluate and fire the deferred close"
        )
        await execution_outcome(runtime, sub, timeout=60.0)
    finally:
        gate.release.set()
        await runtime.shutdown()
