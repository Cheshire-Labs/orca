"""A simulated step lasts its real-world duration times the run's time scale.

Scale 1 is real time, 0 is no pacing and the default. The scale belongs to the
execution, can change while it runs, and never applies to a LIVE run.

Nothing here asserts on the wall clock. The pacer is tested with an injected
sleep that records what was asked of it, and the runtime tests watch the
pacer's `waiting` flag and the steps the dispatcher asks it to pace.

Plate moves are paced too (10 s of real time each), so a test that wants to act
in the middle of a long shake submits at a small scale: 0.05 makes the move to
the station half a second and a 3600 s shake three minutes.
"""

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from uuid import uuid4

import pytest

import orca.orca as orca
from orca.events.event_bus import EventBus
from orca.resource_models.labware import PlateTemplate
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.execution import ExecutionPhase
from orca.runtime.execution_record import ExecutionRecord
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.runtime_interface import SimTimeScaleNotApplicableError
from orca.runtime import sim_pacing
from orca.runtime.sim_pacing import (
    DEFAULT_NOMINAL_STEP_SECONDS,
    NOMINAL_PLATE_MOVE_SECONDS,
    PacingOutcome,
    SimPacer,
    nominal_step_seconds,
    pacer_for_run_mode,
)
from orca.runtime.sinks import CollectorSink
from orca.runtime.submission_modes import BatchMode
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
from tests.test_helpers import create_test_transporter, wire_system_map

MID_STEP_SCALE = 0.05


class _ObservedDevice(UniversalMockDevice):
    """Says when the first shake reached the driver."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.first_shake_started = asyncio.Event()

    async def shake(self, duration: int, speed: int) -> None:
        self.first_shake_started.set()
        await super().shake(duration, speed)


class _PacedStep:
    """One call the dispatcher or a move made on the pacer."""

    def __init__(self, sim_seconds: float, scale: float) -> None:
        self.sim_seconds = sim_seconds
        self.scale = scale


class _Bench:
    """One entry thread that shakes N times; the body records each call's return."""

    def __init__(
        self,
        runtime: SystemRuntime,
        workflow: WorkflowTemplate,
        device: _ObservedDevice,
        sink: CollectorSink,
        shake_calls: list[int],
        paced: list[_PacedStep],
    ) -> None:
        self.runtime = runtime
        self.workflow = workflow
        self.device = device
        self.sink = sink
        self.shake_calls = shake_calls
        self.paced = paced

    def pacer(self, eid: str) -> SimPacer:
        pacer = self.runtime.require_execution(eid).sim_pacer
        assert pacer is not None
        return pacer


def _owner_group() -> LabwareGroup:
    return LabwareGroup(
        id=str(uuid4()),
        members=(LabwareGroupMember(thread_template_name="owner"),),
    )


@pytest.fixture
def paced(monkeypatch: pytest.MonkeyPatch) -> list[_PacedStep]:
    """Every pace() call the run makes, with the scale in force when it began."""
    calls: list[_PacedStep] = []
    original = SimPacer.pace

    async def recording(self: SimPacer, sim_seconds: float, **kwargs: asyncio.Event | None):
        calls.append(_PacedStep(sim_seconds, self.time_scale))
        return await original(self, sim_seconds, **kwargs)

    monkeypatch.setattr(SimPacer, "pace", recording)
    return calls


async def _bench(
    paced: list[_PacedStep], shake_seconds: int, shakes: int = 1,
    default_sim_time_scale: float = 0.0,
) -> _Bench:
    device = _ObservedDevice("station")
    transporter = create_test_transporter("robot1", ["start_pad", "station", "waste"])
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
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    runtime = SystemRuntime(
        system, event_bus=event_bus, default_sim_time_scale=default_sim_time_scale,
    )
    sink = CollectorSink()
    runtime.register_sink(sink)
    await runtime.start()
    return _Bench(runtime, workflow, device, sink, shake_calls, paced)


async def _submit(
    bench: _Bench, *, sim_time_scale: float | None,
    mode: WorkflowRunMode = WorkflowRunMode.PURE_SIM,
) -> str:
    submission = await bench.runtime.submit(
        bench.workflow, groups=[_owner_group()], mode=mode, sim_time_scale=sim_time_scale,
    )
    return submission.execution_id


async def _wait_until(predicate: Callable[[], bool], *, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


async def _standing_in_for_the_operator(bench: _Bench, eid: str) -> ExecutionRecord:
    """Wait for the run. A LIVE thread parks until someone puts its plate on the
    start pad, and again at the end until someone takes it off the waste pad."""
    final = asyncio.ensure_future(bench.runtime.wait(eid))
    placed = False
    while not final.done():
        for thread in bench.runtime.list_threads(eid):
            if thread.status == "AWAITING_MANUAL_PLACE" and not placed:
                await bench.runtime.labware.register("owner", location="start_pad", confirm=True)
                placed = True
            elif thread.status == "AWAITING_MANUAL_REMOVE" and thread.labware_id is not None:
                await bench.runtime.labware.discharge_labware(thread.labware_id, confirm=True)
        await asyncio.sleep(0.05)
    return await final


async def _submit_and_reach_the_paced_shake(bench: _Bench, *, sim_time_scale: float) -> str:
    eid = await _submit(bench, sim_time_scale=sim_time_scale)
    await asyncio.wait_for(bench.device.first_shake_started.wait(), timeout=15.0)
    await _wait_until(
        lambda: bench.pacer(eid).waiting, timeout=10.0, what="the shake to be paced",
    )
    assert not bench.shake_calls, "the shake call returned before the step was paced"
    return eid


def _all_paused(bench: _Bench, eid: str) -> bool:
    threads = bench.runtime.list_threads(eid)
    return bool(threads) and all(t.status == "PAUSED" for t in threads)


class TestAPacedStepLastsItsScaledDuration:

    async def test_the_shake_is_paced_by_its_declared_duration_at_the_runs_scale(
        self, paced: list[_PacedStep],
    ) -> None:
        bench = await _bench(paced, shake_seconds=2)
        try:
            eid = await _submit(bench, sim_time_scale=0.25)
            final = await asyncio.wait_for(bench.runtime.wait(eid), timeout=30.0)
            assert final.status == ExecutionState.COMPLETED
            steps = [p for p in bench.paced if p.scale == 0.25]
            # Two moves at the plate-move nominal and one shake at its declared
            # two seconds, each less the time the sim driver itself took.
            assert len(steps) == 3
            shake = min(steps, key=lambda p: p.sim_seconds)
            assert 0 < shake.sim_seconds <= 2.0
            for move in steps:
                if move is not shake:
                    assert 2.0 < move.sim_seconds <= NOMINAL_PLATE_MOVE_SECONDS
        finally:
            await bench.runtime.shutdown()

    async def test_scale_zero_adds_no_wait(self, paced: list[_PacedStep]) -> None:
        bench = await _bench(paced, shake_seconds=3600)
        try:
            eid = await _submit(bench, sim_time_scale=0.0)
            final = await asyncio.wait_for(bench.runtime.wait(eid), timeout=15.0)
            assert final.status == ExecutionState.COMPLETED
            assert all(p.scale == 0.0 for p in bench.paced)
        finally:
            await bench.runtime.shutdown()

    async def test_the_default_scale_is_zero_so_an_unscaled_submission_is_not_paced(
        self, paced: list[_PacedStep],
    ) -> None:
        bench = await _bench(paced, shake_seconds=3600)
        try:
            eid = await _submit(bench, sim_time_scale=None)
            assert bench.runtime.get_execution_detail(eid).sim_time_scale == 0.0
            final = await asyncio.wait_for(bench.runtime.wait(eid), timeout=15.0)
            assert final.status == ExecutionState.COMPLETED
        finally:
            await bench.runtime.shutdown()

    async def test_the_deployment_default_applies_when_the_submission_names_no_scale(
        self, paced: list[_PacedStep],
    ) -> None:
        bench = await _bench(paced, shake_seconds=2, default_sim_time_scale=0.25)
        try:
            eid = await _submit(bench, sim_time_scale=None)
            assert bench.runtime.get_execution_detail(eid).sim_time_scale == 0.25
            final = await asyncio.wait_for(bench.runtime.wait(eid), timeout=30.0)
            assert final.status == ExecutionState.COMPLETED
            assert any(p.scale == 0.25 for p in bench.paced)
        finally:
            await bench.runtime.shutdown()


class TestAScaleChangeAppliesAtOnce:

    async def test_a_change_mid_step_rescales_the_rest_of_that_step(
        self, paced: list[_PacedStep],
    ) -> None:
        bench = await _bench(paced, shake_seconds=3600)
        try:
            eid = await _submit_and_reach_the_paced_shake(bench, sim_time_scale=MID_STEP_SCALE)

            assert bench.runtime.set_execution_time_scale(eid, 0.0) == 0.0

            final = await asyncio.wait_for(bench.runtime.wait(eid), timeout=10.0)
            assert final.status == ExecutionState.COMPLETED
            assert bench.runtime.get_execution_detail(eid).sim_time_scale == 0.0
            changed = [
                e for e in bench.sink.events
                if e.event_name == f"EXECUTION.{eid}.TIME_SCALE_CHANGED"
            ]
            assert len(changed) == 1
            assert changed[0].to_dict()["context"]["sim_time_scale"] == 0.0
        finally:
            await bench.runtime.shutdown()

    async def test_a_change_between_steps_paces_the_next_step_at_the_new_scale(
        self, paced: list[_PacedStep],
    ) -> None:
        bench = await _bench(paced, shake_seconds=2, shakes=2)
        try:
            eid = await _submit(bench, sim_time_scale=0.0)
            await _wait_until(
                lambda: len(bench.shake_calls) == 1, timeout=10.0, what="the first shake",
            )
            bench.runtime.set_execution_time_scale(eid, 0.25)
            final = await asyncio.wait_for(bench.runtime.wait(eid), timeout=30.0)
            assert final.status == ExecutionState.COMPLETED
            # Every step before the change was paced at 0 (so owed nothing); the
            # one shake-sized step at 0.25 is the second shake, after the change.
            scales = [p.scale for p in bench.paced]
            assert scales[0] == 0.0
            assert scales.index(0.25) > 0
            second_shake = [p for p in bench.paced if p.scale == 0.25 and 0 < p.sim_seconds <= 2.0]
            assert len(second_shake) == 1
        finally:
            await bench.runtime.shutdown()


class TestAPauseStopsTheClock:

    async def test_a_pause_ends_the_wait_at_once_and_resume_keeps_the_remainder(
        self, paced: list[_PacedStep],
    ) -> None:
        bench = await _bench(paced, shake_seconds=3600)
        try:
            eid = await _submit_and_reach_the_paced_shake(bench, sim_time_scale=MID_STEP_SCALE)

            bench.runtime.pause_execution(eid)
            await _wait_until(
                lambda: _all_paused(bench, eid),
                timeout=5.0, what="the thread to pause inside the paced step",
            )
            assert not bench.pacer(eid).waiting
            assert not bench.shake_calls
            [thread] = bench.runtime.list_threads(eid)
            assert thread.pause_message is not None
            assert "simulated step" in thread.pause_message

            # Paused time is not simulated time: the remainder is still owed and
            # runs out only once the run resumes, here at scale 0 so it is quick.
            bench.runtime.set_execution_time_scale(eid, 0.0)
            bench.runtime.resume_execution(eid)
            final = await asyncio.wait_for(bench.runtime.wait(eid), timeout=10.0)
            assert final.status == ExecutionState.COMPLETED
            assert len(bench.shake_calls) == 1
            # The remainder is what ran out after resume: nearly the whole step, at scale 0.
            assert any(p.scale == 0.0 and p.sim_seconds > 3500.0 for p in bench.paced)
        finally:
            await bench.runtime.shutdown()

    async def test_a_confirmed_stop_aborts_a_paced_step_at_once(
        self, paced: list[_PacedStep],
    ) -> None:
        bench = await _bench(paced, shake_seconds=3600)
        try:
            eid = await _submit_and_reach_the_paced_shake(bench, sim_time_scale=MID_STEP_SCALE)

            armed = await asyncio.wait_for(bench.runtime.stop_execution(eid), timeout=5.0)
            assert armed.armed is True
            await _wait_until(
                lambda: _all_paused(bench, eid), timeout=5.0, what="the paused thread",
            )
            aborted = await asyncio.wait_for(
                bench.runtime.stop_execution(eid, confirm=True), timeout=10.0,
            )
            assert aborted.aborted is True
            assert aborted.phase is ExecutionPhase.ABORTED
        finally:
            await bench.runtime.shutdown()

class TestLiveIsNeverPaced:

    async def test_a_live_submission_refuses_a_time_scale(self, paced: list[_PacedStep]) -> None:
        bench = await _bench(paced, shake_seconds=1)
        try:
            with pytest.raises(SimTimeScaleNotApplicableError):
                await _submit(bench, sim_time_scale=1.0, mode=WorkflowRunMode.LIVE)
            assert bench.runtime.list_executions() == []
        finally:
            await bench.runtime.shutdown()

    async def test_a_live_execution_has_no_pacer_even_under_a_deployment_default(
        self, paced: list[_PacedStep],
    ) -> None:
        bench = await _bench(paced, shake_seconds=1, default_sim_time_scale=1.0)
        try:
            eid = await _submit(bench, sim_time_scale=None, mode=WorkflowRunMode.LIVE)
            assert bench.runtime.require_execution(eid).sim_pacer is None
            assert bench.runtime.get_execution_detail(eid).sim_time_scale is None
            with pytest.raises(SimTimeScaleNotApplicableError):
                bench.runtime.set_execution_time_scale(eid, 0.5)
        finally:
            await bench.runtime.shutdown(confirm=True)

    async def test_a_live_run_completes_without_a_single_paced_step(
        self, paced: list[_PacedStep],
    ) -> None:
        bench = await _bench(paced, shake_seconds=3600, default_sim_time_scale=1.0)
        try:
            eid = await _submit(bench, sim_time_scale=None, mode=WorkflowRunMode.LIVE)
            # A LIVE run waits for the operator to place the plate. Once it is there,
            # the hour-long shake must not take an hour under the 1.0 default.
            final = await asyncio.wait_for(_standing_in_for_the_operator(bench, eid), timeout=15.0)
            assert final.status == ExecutionState.COMPLETED
            assert bench.shake_calls == [0]
            assert bench.paced == []
        finally:
            await bench.runtime.shutdown()

    def test_the_thread_seed_is_none_for_a_live_thread(self) -> None:
        """The seed is what keeps every pacing site off a LIVE thread."""
        pacer = SimPacer(1.0)
        assert pacer_for_run_mode(WorkflowRunMode.LIVE, pacer) is None
        assert pacer_for_run_mode(WorkflowRunMode.PURE_SIM, pacer) is pacer
        assert pacer_for_run_mode(WorkflowRunMode.DEVICE_SIM, pacer) is pacer


class TestSettingTheScale:

    async def test_an_unknown_execution_is_a_key_error(self, paced: list[_PacedStep]) -> None:
        bench = await _bench(paced, shake_seconds=1)
        try:
            with pytest.raises(KeyError):
                bench.runtime.set_execution_time_scale("nope", 1.0)
        finally:
            await bench.runtime.shutdown()

    async def test_a_finished_execution_refuses_a_change(self, paced: list[_PacedStep]) -> None:
        bench = await _bench(paced, shake_seconds=1)
        try:
            eid = await _submit(bench, sim_time_scale=0.0)
            await asyncio.wait_for(bench.runtime.wait(eid), timeout=15.0)
            with pytest.raises(RuntimeError):
                bench.runtime.set_execution_time_scale(eid, 1.0)
        finally:
            await bench.runtime.shutdown()

    async def test_a_negative_scale_is_rejected_before_anything_runs(
        self, paced: list[_PacedStep],
    ) -> None:
        bench = await _bench(paced, shake_seconds=1)
        try:
            with pytest.raises(ValueError):
                await _submit(bench, sim_time_scale=-1.0)
            assert bench.runtime.list_executions() == []
        finally:
            await bench.runtime.shutdown()

    async def test_a_joining_submission_may_not_carry_a_scale_even_with_nothing_to_join(
        self, paced: list[_PacedStep],
    ) -> None:
        bench = await _bench(paced, shake_seconds=1)
        try:
            with pytest.raises(SimTimeScaleNotApplicableError):
                await bench.runtime.submit(
                    bench.workflow, groups=[_owner_group()], mode=WorkflowRunMode.PURE_SIM,
                    batch_mode=BatchMode.JOIN_EXISTING, sim_time_scale=0.5,
                )
            assert bench.runtime.list_executions() == []
        finally:
            await bench.runtime.shutdown()

    async def test_a_joining_submission_may_not_carry_a_scale(self, paced: list[_PacedStep]) -> None:
        """The scale belongs to the execution; a join uses the set verb instead."""
        bench = await _bench(paced, shake_seconds=3600)
        try:
            eid = await _submit_and_reach_the_paced_shake(bench, sim_time_scale=MID_STEP_SCALE)
            with pytest.raises(SimTimeScaleNotApplicableError):
                await bench.runtime.submit(
                    bench.workflow, groups=[_owner_group()], mode=WorkflowRunMode.PURE_SIM,
                    batch_mode=BatchMode.JOIN_EXISTING, sim_time_scale=0.5,
                )
            bench.runtime.set_execution_time_scale(eid, 0.0)
            await asyncio.wait_for(bench.runtime.wait(eid), timeout=10.0)
        finally:
            await bench.runtime.shutdown()

    async def test_a_change_right_after_submit_still_reaches_the_sinks(
        self, paced: list[_PacedStep],
    ) -> None:
        """The run task has not started yet; the event must not be dropped for it."""
        bench = await _bench(paced, shake_seconds=1)
        try:
            eid = await _submit(bench, sim_time_scale=1.0)
            bench.runtime.set_execution_time_scale(eid, 0.0)
            final = await asyncio.wait_for(bench.runtime.wait(eid), timeout=15.0)
            assert final.status == ExecutionState.COMPLETED
            names = [event.event_name for event in bench.sink.events]
            assert f"EXECUTION.{eid}.TIME_SCALE_CHANGED" in names, names
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


class _Sleeper:
    """Records every sleep the pacer asks for and sleeps only until released."""

    def __init__(self) -> None:
        self.requested: list[float] = []
        self.release = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.requested.append(seconds)
        await self.release.wait()


class TestThePacerAlone:

    async def test_scale_zero_asks_for_no_sleep(self) -> None:
        sleeper = _Sleeper()
        result = await SimPacer(0.0, sleep=sleeper).pace(3600.0)
        assert result.outcome is PacingOutcome.ELAPSED
        assert sleeper.requested == []

    async def test_the_wait_is_the_simulated_time_at_the_scale(self) -> None:
        sleeper = _Sleeper()
        pacer = SimPacer(0.25, sleep=sleeper)
        task = asyncio.create_task(pacer.pace(2.0))
        await _wait_until(lambda: pacer.waiting, timeout=2.0, what="the pacer to park")
        assert sleeper.requested == [0.5]
        sleeper.release.set()
        result = await asyncio.wait_for(task, timeout=2.0)
        assert result.outcome is PacingOutcome.ELAPSED
        assert not pacer.waiting

    async def test_a_change_mid_wait_re_plans_at_the_new_scale(self) -> None:
        sleeper = _Sleeper()
        pacer = SimPacer(1.0, sleep=sleeper)
        task = asyncio.create_task(pacer.pace(3600.0))
        await _wait_until(lambda: pacer.waiting, timeout=2.0, what="the pacer to park")
        assert sleeper.requested == [3600.0]

        pacer.set_time_scale(0.5)
        await _wait_until(lambda: len(sleeper.requested) == 2, timeout=2.0, what="the re-plan")
        # The remainder is near the whole step, now at half the wall time.
        assert 1700.0 < sleeper.requested[1] <= 1800.0

        pacer.set_time_scale(0.0)
        result = await asyncio.wait_for(task, timeout=2.0)
        assert result.outcome is PacingOutcome.ELAPSED
        assert len(sleeper.requested) == 2

    async def test_a_change_the_instant_a_wait_starts_is_not_missed(self) -> None:
        sleeper = _Sleeper()
        pacer = SimPacer(1.0, sleep=sleeper)
        task = asyncio.create_task(pacer.pace(3600.0))
        # One loop turn: the wait has picked its sleep but its racers have not
        # started running yet. A change landing right here must still wake it.
        await asyncio.sleep(0)
        assert sleeper.requested == []
        pacer.set_time_scale(0.0)
        result = await asyncio.wait_for(task, timeout=2.0)
        assert result.outcome is PacingOutcome.ELAPSED

    async def test_a_pause_request_returns_what_is_still_owed(self) -> None:
        sleeper = _Sleeper()
        pacer = SimPacer(0.1, sleep=sleeper)
        pause = asyncio.Event()
        task = asyncio.create_task(pacer.pace(100.0, pause_requested=pause))
        await _wait_until(lambda: pacer.waiting, timeout=2.0, what="the pacer to park")
        pause.set()
        result = await asyncio.wait_for(task, timeout=2.0)
        assert result.outcome is PacingOutcome.PAUSE_REQUESTED
        assert 90.0 < result.remaining_sim_seconds <= 100.0

    def test_a_bad_scale_is_rejected(self) -> None:
        for bad in (-0.1, float("nan"), float("inf"), True):
            with pytest.raises(ValueError):
                SimPacer(bad)

    def test_the_real_pacer_sleeps_with_asyncio(self) -> None:
        assert SimPacer(1.0)._sleep is sim_pacing.asyncio.sleep
