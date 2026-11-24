"""A simulated run carries a speed-up: who may set it, when, and what a pause or a stop does to a command in progress.

The speed-up belongs to the execution, can change while it runs, and applies only to a
sim run, never to a LIVE run or a check run. The wait itself is the device simulator's,
so nothing here waits on the wall clock: a device holds its command until the test lets it go.
"""


from orca.config import OrcaConfig
import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cheshire_drivers.command_timings import collect_command_timings
from cheshire_drivers.interfaces import ITransporterDriver

import orca.orca as orca
from orca.events.event_bus import EventBus
from orca.resource_models.labware import PlateTemplate
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.execution import ExecutionPhase
from orca.runtime.execution_record import ExecutionRecord
from orca.workflow_models.labware_group import LabwareGroup, LabwareGroupMember
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.errors import SimSpeedupNotApplicableError
from orca.modes import sim_pacing
from orca.modes.sim_pacing import (
    DEFAULT_NOMINAL_STEP_SECONDS,
    SimPacer,
    nominal_step_seconds,
    pacer_for_run_mode,
)
from orca.runtime.sinks import CollectorSink
from orca.workflow_models.submission_modes import BatchMode
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import ThreadTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate, MethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from tests.mock import UniversalMockDevice
from tests.test_helpers import create_test_transporter, let_the_loop_run, wire_system_map
from tests.no_devices import NullGatewayRegistry
from tests.runtime.manual_place_fixtures import _live_connection_source
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.memory_activity import memory_unsaved_positions
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service

class _ObservedDevice(UniversalMockDevice):
    """Says when the first shake reached the driver."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.first_shake_started = asyncio.Event()

    async def shake(self, duration: int, speed: int) -> None:
        self.first_shake_started.set()
        await super().shake(duration, speed)


class _HeldDevice(_ObservedDevice):
    """Its shake does not return until the test lets it go."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.let_go = asyncio.Event()

    async def shake(self, duration: int, speed: int) -> None:
        self.first_shake_started.set()
        await self.let_go.wait()
        await UniversalMockDevice.shake(self, duration, speed)


class _CountingDevice(_HeldDevice):
    """Counts the shakes that reached the driver."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.shakes_started = 0

    async def shake(self, duration: int, speed: int) -> None:
        self.shakes_started += 1
        await super().shake(duration, speed)


class _Bench:
    """One entry thread that shakes N times; the body records each call's return."""

    def __init__(
        self,
        runtime: SystemRuntime,
        workflow: WorkflowTemplate,
        device: _ObservedDevice,
        sink: CollectorSink,
        shake_calls: list[int],
    ) -> None:
        self.runtime = runtime
        self.workflow = workflow
        self.device = device
        self.sink = sink
        self.shake_calls = shake_calls



def _owner_group() -> LabwareGroup:
    return LabwareGroup(
        id=str(uuid4()),
        members=(LabwareGroupMember(thread_template_name="owner"),),
    )


async def _bench(
    shake_seconds: int, shakes: int = 1,
    default_sim_speedup: float | None = None, device_cls: type[_ObservedDevice] = _ObservedDevice,
) -> _Bench:
    device = device_cls("station")
    transporter = create_test_transporter("robot1", ["start_pad", "station", "waste"], single_carriage=True)
    owner_plate = PlateTemplate("owner", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    station_pool = ResourcePool("station", [device])
    registry.add_resource_pool(station_pool)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map, devices={"station": device}, pads=["start_pad", "waste"],
    )

    shake_calls: list[int] = []

    @orca.action(device=station_pool, inputs=[owner_plate])
    async def shake_action(ctx: ActionContext) -> None:
        for index in range(shakes):
            await ctx.device().shake(duration=shake_seconds, speed=500)
            shake_calls.append(index)

    async def _method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_action
    shake_method = MethodTemplate("shake_method", func=_method)

    start_pad = system_map.get_location("start_pad")
    waste = system_map.get_location("waste")

    async def _owner_func(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield shake_method
    owner_thread = ThreadTemplate(
        labware_template=owner_plate, start=start_pad, end=waste, func=_owner_func,
    )

    workflow = WorkflowTemplate("paced_wf")
    workflow.add_thread(owner_thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="paced_system",
        description="",
        labwares=[owner_plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus, config=OrcaConfig(), ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(),
        unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    system = builder.get_system()
    runtime = await memory_runtime(
        system, event_bus=event_bus, default_sim_speedup=default_sim_speedup,
        gateway_registry=NullGatewayRegistry(), connection_source=_live_connection_source(("station",)),
    )
    sink = CollectorSink()
    runtime.register_sink(sink)
    await runtime.start()
    return _Bench(runtime, workflow, device, sink, shake_calls)


async def _submit(
    bench: _Bench, *, sim_speedup: float | None,
    mode: WorkflowRunMode = WorkflowRunMode.SIM,
) -> str:
    submission = await bench.runtime.submit(
        bench.workflow, groups=[_owner_group()], mode=mode, sim_speedup=sim_speedup,
    )
    return submission.execution_id


async def _wait_until(predicate: Callable[[], bool], *, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


async def _submit_and_reach_the_held_shake(bench: _Bench) -> tuple[str, _HeldDevice]:
    device = bench.device
    assert isinstance(device, _HeldDevice)
    # The held device waits on the test, not the clock; the arm's simulator waits each move out at this speed-up.
    eid = await _submit(bench, sim_speedup=1000.0)
    await asyncio.wait_for(device.first_shake_started.wait(), timeout=15.0)
    return eid, device


def _all_paused(bench: _Bench, eid: str) -> bool:
    threads = bench.runtime.list_threads(eid)
    return bool(threads) and all(t.status == "PAUSED" for t in threads)


class TestTheSpeedupARunStartsWith:

    async def test_a_submission_naming_none_has_none_when_the_deployment_has_no_default(self) -> None:
        bench = await _bench(shake_seconds=3600)
        try:
            eid = await _submit(bench, sim_speedup=None)
            detail = bench.runtime.get_execution_detail(eid)
            assert (detail.simulated, detail.sim_speedup) == (True, None)
            final = await asyncio.wait_for(bench.runtime.wait(eid), timeout=15.0)
            assert final.status == ExecutionState.COMPLETED
        finally:
            await bench.runtime.shutdown()

    async def test_the_deployment_default_applies_when_the_submission_names_no_speedup(self) -> None:
        bench = await _bench(shake_seconds=2, default_sim_speedup=4.0)
        try:
            eid = await _submit(bench, sim_speedup=None)
            assert bench.runtime.get_execution_detail(eid).sim_speedup == 4.0
        finally:
            await bench.runtime.shutdown()


class TestAPauseLetsACommandInProgressFinish:

    async def test_a_pause_waits_for_the_command_then_holds_before_the_next_one(self) -> None:
        bench = await _bench(shake_seconds=3600, device_cls=_HeldDevice)
        try:
            eid, device = await _submit_and_reach_the_held_shake(bench)

            bench.runtime.pause_execution(eid)
            await let_the_loop_run()
            assert not _all_paused(bench, eid)

            device.let_go.set()
            await _wait_until(
                lambda: _all_paused(bench, eid), timeout=10.0, what="the thread to pause after the command",
            )
            [thread] = bench.runtime.list_threads(eid)
            assert thread.pause_message is not None
            assert "between instrument commands" in thread.pause_message

            bench.runtime.resume_execution(eid)
            final = await asyncio.wait_for(bench.runtime.wait(eid), timeout=10.0)
            assert final.status == ExecutionState.COMPLETED
            assert bench.shake_calls == [0]
        finally:
            await bench.runtime.shutdown()

    async def test_a_confirmed_stop_aborts_a_command_in_progress_at_once(self) -> None:
        bench = await _bench(shake_seconds=3600, device_cls=_HeldDevice)
        try:
            eid, _ = await _submit_and_reach_the_held_shake(bench)

            armed = await asyncio.wait_for(bench.runtime.stop_execution(eid), timeout=5.0)
            assert armed.armed is True
            aborted = await asyncio.wait_for(
                bench.runtime.stop_execution(eid, confirm=True), timeout=10.0,
            )
            assert aborted.aborted is True
            assert aborted.phase is ExecutionPhase.ABORTED
        finally:
            await bench.runtime.shutdown()


class TestAPauseHoldsBetweenInstrumentCommands:

    async def _paused_after_the_first_shake(self) -> tuple[_Bench, _CountingDevice, str]:
        bench = await _bench(shake_seconds=2, shakes=3, device_cls=_CountingDevice)
        device = bench.device
        assert isinstance(device, _CountingDevice)
        # The counting device waits on the test; the arm's simulator waits each move out at this speed-up.
        eid = await _submit(bench, sim_speedup=1000.0)
        await asyncio.wait_for(device.first_shake_started.wait(), timeout=15.0)
        bench.runtime.pause_execution(eid)
        device.let_go.set()
        await _wait_until(lambda: _all_paused(bench, eid), timeout=5.0, what="the thread to pause")
        return bench, device, eid

    async def test_a_pause_during_a_command_stops_the_action_before_its_next_command(
        self,
    ) -> None:
        bench, device, eid = await self._paused_after_the_first_shake()
        try:
            assert device.shakes_started == 1
            [thread] = bench.runtime.list_threads(eid)
            assert thread.pause_message is not None
            assert "between instrument commands" in thread.pause_message

            bench.runtime.resume_execution(eid)
            final = await asyncio.wait_for(bench.runtime.wait(eid), timeout=15.0)
            assert final.status == ExecutionState.COMPLETED
            assert bench.shake_calls == [0, 1, 2]
        finally:
            await bench.runtime.shutdown()

    async def test_a_confirmed_stop_during_the_hold_runs_no_more_commands(
        self,
    ) -> None:
        bench, device, eid = await self._paused_after_the_first_shake()
        try:
            armed = await asyncio.wait_for(bench.runtime.stop_execution(eid), timeout=5.0)
            assert armed.armed is True
            aborted = await asyncio.wait_for(bench.runtime.stop_execution(eid, confirm=True), timeout=10.0)
            assert aborted.phase is ExecutionPhase.ABORTED
            await let_the_loop_run()
            assert device.shakes_started == 1
        finally:
            await bench.runtime.shutdown()


class TestLiveIsNeverPaced:

    async def test_a_live_submission_refuses_a_speedup(self) -> None:
        bench = await _bench(shake_seconds=1)
        try:
            with pytest.raises(SimSpeedupNotApplicableError):
                await _submit(bench, sim_speedup=1.0, mode=WorkflowRunMode.LIVE)
            assert bench.runtime.list_executions() == []
        finally:
            await bench.runtime.shutdown()

    async def test_a_live_execution_has_no_pacer_even_under_a_deployment_default(
        self,
    ) -> None:
        bench = await _bench(shake_seconds=1, default_sim_speedup=1.0)
        try:
            eid = await _submit(bench, sim_speedup=None, mode=WorkflowRunMode.LIVE)
            assert bench.runtime.require_execution(eid).sim_pacer is None
            assert bench.runtime.get_execution_detail(eid).sim_speedup is None
            with pytest.raises(SimSpeedupNotApplicableError):
                bench.runtime.set_execution_sim_speedup(eid, 2.0)
        finally:
            await bench.runtime.shutdown(confirm=True)

    def test_the_thread_seed_is_none_for_a_live_or_check_run_thread(self) -> None:
        """The seed is what keeps every pacing site off a LIVE or check-run thread."""
        pacer = SimPacer(1.0)
        assert pacer_for_run_mode(WorkflowRunMode.LIVE, pacer) is None
        assert pacer_for_run_mode(WorkflowRunMode.CHECK_RUN, pacer) is None
        assert pacer_for_run_mode(WorkflowRunMode.SIM, pacer) is pacer


class TestSettingTheSpeedup:

    async def test_an_unknown_execution_is_a_key_error(self) -> None:
        bench = await _bench(shake_seconds=1)
        try:
            with pytest.raises(KeyError):
                bench.runtime.set_execution_sim_speedup("nope", 1.0)
        finally:
            await bench.runtime.shutdown()

    async def test_a_finished_execution_refuses_a_change(self) -> None:
        bench = await _bench(shake_seconds=1)
        try:
            eid = await _submit(bench, sim_speedup=None)
            await asyncio.wait_for(bench.runtime.wait(eid), timeout=15.0)
            with pytest.raises(RuntimeError):
                bench.runtime.set_execution_sim_speedup(eid, 1.0)
        finally:
            await bench.runtime.shutdown()

    @pytest.mark.parametrize("bad", [0.0, 0.05, 1001.0, -1.0])
    async def test_a_speedup_outside_its_range_is_rejected_before_anything_runs(
        self, bad: float,
    ) -> None:
        bench = await _bench(shake_seconds=1)
        try:
            with pytest.raises(ValueError):
                await _submit(bench, sim_speedup=bad)
            assert bench.runtime.list_executions() == []
        finally:
            await bench.runtime.shutdown()

    async def test_a_joining_submission_may_not_carry_a_speedup_even_with_nothing_to_join(
        self,
    ) -> None:
        bench = await _bench(shake_seconds=1)
        try:
            with pytest.raises(SimSpeedupNotApplicableError):
                await bench.runtime.submit(
                    bench.workflow, groups=[_owner_group()], mode=WorkflowRunMode.SIM,
                    batch_mode=BatchMode.JOIN_EXISTING, sim_speedup=2.0,
                )
            assert bench.runtime.list_executions() == []
        finally:
            await bench.runtime.shutdown()

    async def test_a_joining_submission_may_not_carry_a_speedup(self) -> None:
        """The speed-up belongs to the execution; a join uses the set verb instead."""
        bench = await _bench(shake_seconds=3600, device_cls=_HeldDevice)
        try:
            eid, device = await _submit_and_reach_the_held_shake(bench)
            with pytest.raises(SimSpeedupNotApplicableError):
                await bench.runtime.submit(
                    bench.workflow, groups=[_owner_group()], mode=WorkflowRunMode.SIM,
                    batch_mode=BatchMode.JOIN_EXISTING, sim_speedup=2.0,
                )
            device.let_go.set()
            await asyncio.wait_for(bench.runtime.wait(eid), timeout=10.0)
        finally:
            await bench.runtime.shutdown()

    async def test_a_change_right_after_submit_still_reaches_the_sinks(
        self,
    ) -> None:
        """The run task has not started yet; the event must not be dropped for it."""
        bench = await _bench(shake_seconds=1)
        try:
            eid = await _submit(bench, sim_speedup=1.0)
            bench.runtime.set_execution_sim_speedup(eid, 1000.0)
            final = await asyncio.wait_for(bench.runtime.wait(eid), timeout=15.0)
            assert final.status == ExecutionState.COMPLETED
            names = [event.event_name for event in bench.sink.events]
            assert f"EXECUTION.{eid}.SIM_SPEEDUP_CHANGED" in names, names
        finally:
            await bench.runtime.shutdown()


class TestNominalStepSeconds:

    def test_the_calls_own_duration_argument_wins(self) -> None:
        async def shake(duration: int, speed: int) -> None: ...
        assert nominal_step_seconds(shake, (), {"duration": 90, "speed": 500}, typical_seconds=60.0) == 90.0
        assert nominal_step_seconds(shake, (120, 500), {}, typical_seconds=60.0) == 120.0

    def test_a_duration_field_on_a_request_object_wins(self) -> None:
        class Request:
            duration = 45.0

        async def seal(request: Request) -> None: ...
        assert nominal_step_seconds(seal, (Request(),), {}, typical_seconds=90.0) == 45.0

    def test_the_interfaces_typical_seconds_otherwise(self) -> None:
        async def read(protocol: str) -> None: ...
        assert nominal_step_seconds(read, ("p",), {}, typical_seconds=120.0) == 120.0

    def test_the_fallback_when_nothing_is_declared(self) -> None:
        async def poke() -> None: ...
        assert nominal_step_seconds(poke, (), {}, typical_seconds=None) == DEFAULT_NOMINAL_STEP_SECONDS


@pytest.mark.parametrize("bad", [0.0, -1.0, 0.05, 1001.0, float("inf"), float("nan"), True])
def test_a_pacer_rejects_a_speedup_outside_the_range(bad: float) -> None:
    with pytest.raises(ValueError):
        SimPacer(bad)
