"""A thread whose co-labware does not arrive in time pauses at CO_LABWARE_WAIT.

Plate A's combine needs plate B too. B is held by a manual step, so A's wait for it runs out and A pauses.
RETRY waits again with the whole timeout. ABORT_THREAD ends A, and a contributor that already arrived ends with
it. A keeps the shared device while paused, and a run stop at the pause ends the run.
"""

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass

import orca.orca as orca
from orca.config import CoordinationConfig, OrcaConfig
from orca.events.event_bus import EventBus
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.labware import AnyLabwareTemplate, LabwareTemplate
from orca.resource_models.location import Location
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.execution_record import ExecutionRecord, ExecutionState
from orca.runtime.incident_store import IncidentCategory
from orca.runtime.sinks import CollectorSink
from orca.runtime.status_models import ThreadSnapshot
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.status_enums import PauseSite, RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service, memory_unsaved_positions
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.mock import UniversalMockDevice
from tests.test_helpers import create_test_plate_template, create_test_transporter, wait_for_runtime_condition, wire_system_map

TIMEOUT_S = 1.5
CONTRIBUTOR_TIMEOUT_S = 6.0
COMBINE_SPEED = 500
RIVAL_SPEED = 700


class _CountsShakes(UniversalMockDevice):
    def __init__(self, name: str, site_names: list[str]) -> None:
        super().__init__(name, site_names=site_names)
        self.speeds: list[int] = []

    async def shake(self, duration: int, speed: int) -> None:
        self.speeds.append(speed)
        await super().shake(duration, speed)


@dataclass
class _Lab:
    runtime: SystemRuntime
    shared: _CountsShakes
    record: ExecutionRecord
    events: CollectorSink

    def thread(self, labware: str) -> ThreadSnapshot:
        [found] = [t for t in self.runtime.get_execution_detail(self.record.id).threads if t.name.startswith(labware)]
        return found

    def pauses_of(self, labware: str) -> list[float]:
        thread_id = self.thread(labware).id
        return [e.timestamp for e in self.events.events if e.event_name == f"THREAD.{thread_id}.PAUSED"]

    async def until(self, check: Callable[[], bool], what: str) -> None:
        await wait_for_runtime_condition(self.runtime, check, timeout=10.0, message=what)

    def has_thread(self, labware: str) -> bool:
        return any(t.name.startswith(labware) for t in self.runtime.list_threads(self.record.id))

    async def until_paused(self, labware: str, count: int) -> ThreadSnapshot:
        await self.until(lambda: self.has_thread(labware) and len(self.pauses_of(labware)) == count, f"pause {count}")
        return self.thread(labware)

    def arrived(self, labware: str) -> bool:
        if not self.has_thread(labware):
            return False
        thread = self.thread(labware)
        return (thread.current_location or "").startswith("shared") and thread.status != "MOVING"

    async def confirm(self, instruction: str) -> None:
        def pending() -> list[str]:
            return [s.step_id for s in self.runtime.list_pending_manual_steps(self.record.id) if s.instruction == instruction]

        await self.until(lambda: bool(pending()), f"the manual step {instruction!r}")
        await self.runtime.confirm_manual_step(self.record.id, pending()[0])

    async def release_plate_b(self) -> None:
        await self.confirm("release b")
        await self.until(lambda: self.arrived("plate_b"), "plate b to reach the shared device")

    async def end(self) -> ExecutionRecord:
        return await asyncio.wait_for(self.runtime.wait(self.record.id), timeout=20.0)


async def _lab(*, arrived_contributor: bool = False, rival: bool = False, timeout_s: float = TIMEOUT_S) -> _Lab:
    shared = _CountsShakes("shared", site_names=["site-1", "site-2", "site-3", "site-4"])
    holder = UniversalMockDevice("holder")
    rival_holder = UniversalMockDevice("rival_holder")
    pads = ["pad1", "pad2", "pad3", "pad4"]
    transporter = create_test_transporter("robot1", ["shared", "holder", "rival_holder", *pads])
    plate_a = create_test_plate_template("plate_a")
    plate_b = create_test_plate_template("plate_b")
    plate_c = create_test_plate_template("plate_c")
    plate_d = create_test_plate_template("plate_d")
    registry = ResourceRegistry()
    for resource in (shared, holder, rival_holder, transporter):
        registry.add_resource(resource)
    shared_pool = ResourcePool("shared", [shared])
    holder_pool = ResourcePool("holder", [holder])
    rival_holder_pool = ResourcePool("rival_holder", [rival_holder])
    for pool in (shared_pool, holder_pool, rival_holder_pool):
        registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(
        system_map, devices={"shared": shared, "holder": holder, "rival_holder": rival_holder}, pads=pads,
    )
    combine_inputs: list[LabwareTemplate | AnyLabwareTemplate] = [plate_a, plate_b, plate_c] if arrived_contributor else [plate_a, plate_b]

    @orca.action(device=holder_pool, inputs=[plate_b])
    async def hold_b(ctx: ActionContext) -> None:
        await ctx.manual_step("release b")

    @orca.action(device=shared_pool, inputs=combine_inputs)
    async def combine(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=COMBINE_SPEED)

    @orca.action(device=rival_holder_pool, inputs=[plate_d])
    async def hold_d(ctx: ActionContext) -> None:
        await ctx.manual_step("release d")

    @orca.action(device=shared_pool, inputs=[plate_d])
    async def rival_shake(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=RIVAL_SPEED)

    @orca.method
    async def hold_b_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield hold_b

    @orca.method
    async def combine_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield combine

    @orca.method
    async def rival_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield hold_d
        yield rival_shake

    def pad(name: str) -> Location:
        return system_map.get_location(name)

    @orca.thread(labware=plate_a, start=pad("pad1"), end=pad("pad1"))
    async def thread_a(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield combine_method

    @orca.thread(labware=plate_b, start=pad("pad2"), end=pad("pad2"))
    async def thread_b(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield hold_b_method
        yield orca.join(allows=[combine_method])

    @orca.thread(labware=plate_c, start=pad("pad3"), end=pad("pad3"))
    async def thread_c(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join(allows=[combine_method])

    @orca.thread(labware=plate_d, start=pad("pad4"), end=pad("pad4"))
    async def thread_d(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield rival_method

    @orca.workflow(name="late_co_labware")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(thread_a)
        wf.thread(thread_b)
        if arrived_contributor:
            wf.thread(thread_c)
        if rival:
            wf.start(thread_d)

    labwares = [plate_a, plate_b] + ([plate_c] if arrived_contributor else []) + ([plate_d] if rival else [])
    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="late", description="", labwares=labwares, resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=event_bus,
        config=OrcaConfig(coordination=CoordinationConfig(co_labware_timeout=timeout_s)),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    runtime = await memory_runtime(builder.get_system(), event_bus=event_bus)
    events = CollectorSink()
    runtime.register_sink(events)
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.CHECK_RUN)
    return _Lab(runtime, shared, record, events)


async def test_retry_restarts_the_wait_with_the_whole_timeout_and_the_action_runs_once_the_plate_arrives() -> None:
    lab = await _lab()
    try:
        paused = await lab.until_paused("plate_a", 1)
        assert paused.pause_site == PauseSite.CO_LABWARE_WAIT.value
        assert paused.honoured_decisions == ("RETRY", "ABORT_THREAD")
        assert "plate_b" in (paused.last_error or "")
        [incident] = await lab.runtime.incidents.list(category=IncidentCategory.CO_LABWARE_TIMEOUT)
        assert incident.thread_id == paused.id

        retried_at = time.time()
        lab.runtime.recover_thread(lab.record.id, paused.id, RecoveryDecision.RETRY)
        await lab.until_paused("plate_a", 2)
        assert lab.pauses_of("plate_a")[1] - retried_at >= TIMEOUT_S * 0.9, "the retried wait got the whole timeout"

        await lab.release_plate_b()
        lab.runtime.recover_thread(lab.record.id, paused.id, RecoveryDecision.RETRY)
        final = await lab.end()

        assert final.status is ExecutionState.COMPLETED
        assert lab.shared.speeds == [COMBINE_SPEED], "the combined action ran once"
    finally:
        await lab.runtime.shutdown()


async def test_abort_thread_at_the_pause_ends_a_contributor_that_already_arrived() -> None:
    lab = await _lab(arrived_contributor=True, timeout_s=CONTRIBUTOR_TIMEOUT_S)
    try:
        await lab.until(lambda: lab.arrived("plate_c"), "plate c to arrive before the timeout")
        paused = await lab.until_paused("plate_a", 1)
        assert "plate_b" in (paused.last_error or "") and "plate_c" not in (paused.last_error or "")

        lab.runtime.recover_thread(lab.record.id, paused.id, RecoveryDecision.ABORT_THREAD)
        await lab.until(
            lambda: lab.thread("plate_a").status == "ABORTED" and lab.thread("plate_c").status == "ABORTED",
            "the owner and the arrived contributor to end",
        )
        assert lab.shared.speeds == []
    finally:
        await lab.runtime.shutdown()


async def test_the_paused_thread_keeps_the_device_until_its_action_runs() -> None:
    lab = await _lab(rival=True)
    try:
        paused = await lab.until_paused("plate_a", 1)
        await lab.confirm("release d")
        await lab.until(lambda: "shared" in (lab.thread("plate_d").waiting_for or ""), "plate d to queue for the device")
        assert lab.shared.speeds == [], "plate d did not get the device from the paused thread"

        await lab.release_plate_b()
        lab.runtime.recover_thread(lab.record.id, paused.id, RecoveryDecision.RETRY)
        final = await lab.end()

        assert final.status is ExecutionState.COMPLETED
        assert lab.shared.speeds == [COMBINE_SPEED, RIVAL_SPEED]
    finally:
        await lab.runtime.shutdown()


async def test_a_run_stop_at_the_pause_ends_the_run() -> None:
    lab = await _lab()
    try:
        await lab.until_paused("plate_a", 1)
        await asyncio.wait_for(lab.runtime.stop_execution(lab.record.id), timeout=10.0)
        await asyncio.wait_for(lab.runtime.stop_execution(lab.record.id, confirm=True), timeout=20.0)
        final = await lab.end()

        assert final.status is ExecutionState.ABORTED
        assert lab.shared.speeds == []
    finally:
        await lab.runtime.shutdown()
