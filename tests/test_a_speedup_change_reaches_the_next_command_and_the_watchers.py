"""A change to a running sim run's speed-up paces the next command the run sends, and tells the watchers once."""

import asyncio

from orca.events.execution_context import DeviceCommandContext, ExecutionSimSpeedupContext
from orca.runtime.sinks import CollectorSink
from orca.runtime.system_runtime import ExecutionState
from tests.real_device_path import the_real_device_path
from tests.test_a_runs_sim_speedup import _bench, _HeldDevice, _submit


def _arm_speedups(commands: CollectorSink) -> list[float | None]:
    return [
        event.context.sim_speedup for event in commands.events
        if isinstance(event.context, DeviceCommandContext)
        and event.context.device_name == "robot1" and event.status == "STARTED"
    ]


async def test_a_change_between_two_commands_sends_the_next_command_at_the_new_speedup() -> None:
    commands = CollectorSink()
    the_real_device_path().events.add_sink(commands)
    bench = await _bench(shake_seconds=3600, device_cls=_HeldDevice)
    device = bench.device
    assert isinstance(device, _HeldDevice)
    try:
        eid = await _submit(bench, sim_speedup=None)
        await asyncio.wait_for(device.first_shake_started.wait(), timeout=15.0)
        before = _arm_speedups(commands)

        bench.runtime.set_execution_sim_speedup(eid, 500.0)
        device.let_go.set()
        final = await asyncio.wait_for(bench.runtime.wait(eid), timeout=15.0)

        assert final.status == ExecutionState.COMPLETED
        after = _arm_speedups(commands)[len(before):]
        assert before and set(before) == {None}
        assert after and set(after) == {500.0}
    finally:
        await bench.runtime.shutdown()


async def test_a_change_sends_one_event_that_carries_the_new_speedup() -> None:
    bench = await _bench(shake_seconds=3600, device_cls=_HeldDevice)
    device = bench.device
    assert isinstance(device, _HeldDevice)
    try:
        eid = await _submit(bench, sim_speedup=1000.0)
        await asyncio.wait_for(device.first_shake_started.wait(), timeout=15.0)

        assert bench.runtime.set_execution_sim_speedup(eid, 500.0) == 500.0
        device.let_go.set()
        await asyncio.wait_for(bench.runtime.wait(eid), timeout=15.0)

        changed = [e.context for e in bench.sink.events if e.event_name == f"EXECUTION.{eid}.SIM_SPEEDUP_CHANGED"]
        assert len(changed) == 1
        assert isinstance(changed[0], ExecutionSimSpeedupContext)
        assert (changed[0].execution_id, changed[0].sim_speedup) == (eid, 500.0)
    finally:
        await bench.runtime.shutdown()
