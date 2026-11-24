"""`ctx.wait_for` holds the thread until the event, and continues at once on an earlier emit.

Timeout then RETRY: `test_wait_for_retry_waits_again.py`.
"""

import asyncio

import pytest
from pydantic import JsonValue

import orca.orca as orca
from orca.devices.device_interfaces import IShaker
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.sdk_features.kit import (
    Lab,
    build_world,
    close_world,
    commands,
    new_lab,
    plate_template,
    submit,
    wait_for_the_end,
)
from tests.test_helpers import let_the_loop_run, wait_until

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


class _Signals:
    def __init__(self) -> None:
        self.waiting = asyncio.Event()
        self.release_the_emit = asyncio.Event()
        self.received: list[tuple[str | None, dict[str, JsonValue]]] = []
        self.emitted_at: float | None = None


def _waiter_and_emitter(lab: Lab, signals: _Signals, *, emit_first: bool, timeout: float | None) -> WorkflowTemplate:
    waiter, emitter = plate_template("waiter"), plate_template("emitter")

    @orca.action(device=lab.shaker_1, inputs=[waiter])
    async def wait_then_shake(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=100)
        if emit_first:
            await signals.release_the_emit.wait()
            await wait_until(lambda: signals.emitted_at is not None, timeout=30)
        signals.waiting.set()
        signals.received.append(await ctx.wait_for("go", timeout=timeout))
        await ctx.device(IShaker).shake(duration=1, speed=200)

    @orca.action(device=lab.shaker_2, inputs=[emitter])
    async def emit_go(ctx: ActionContext) -> None:
        await signals.release_the_emit.wait()
        await ctx.emit("go", value="now", data={"lot": 7})
        signals.emitted_at = asyncio.get_running_loop().time()

    @orca.method
    async def wait_method(ctx: MethodContext):
        yield wait_then_shake

    @orca.method
    async def emit_method(ctx: MethodContext):
        yield emit_go

    @orca.thread(labware=waiter, start="pad_1", end="pad_1")
    async def waiter_journey(ctx: ThreadContext):
        yield wait_method

    @orca.thread(labware=emitter, start="pad_2", end="pad_2")
    async def emitter_journey(ctx: ThreadContext):
        yield emit_method

    @orca.workflow(name="wait_for_go")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(waiter_journey)
        wf.start(emitter_journey)

    return workflow



async def test_nothing_after_the_wait_runs_until_the_event_is_emitted() -> None:
    lab = new_lab()
    signals = _Signals()
    world = await build_world(lab, _waiter_and_emitter(lab, signals, emit_first=False, timeout=None))
    try:
        execution_id = await submit(world)
        await asyncio.wait_for(signals.waiting.wait(), timeout=30)
        await let_the_loop_run()
        assert [c.args["speed"] for c in commands(world, "shaker_1")] == [100.0]

        signals.release_the_emit.set()
        ended = await wait_for_the_end(world, execution_id)

        assert set(ended.values()) == {"COMPLETED"}, ended
        assert signals.received == [("now", {"lot": 7})]
        assert [c.args["speed"] for c in commands(world, "shaker_1")] == [100.0, 200.0]
    finally:
        await close_world(world)


async def test_an_event_emitted_before_the_wait_lets_it_continue_at_once() -> None:
    lab = new_lab()
    signals = _Signals()
    signals.release_the_emit.set()
    world = await build_world(lab, _waiter_and_emitter(lab, signals, emit_first=True, timeout=5.0))
    try:
        execution_id = await submit(world)
        ended = await wait_for_the_end(world, execution_id)

        assert set(ended.values()) == {"COMPLETED"}, ended
        assert signals.received == [("now", {"lot": 7})]
        assert [c.args["speed"] for c in commands(world, "shaker_1")] == [100.0, 200.0]
    finally:
        await close_world(world)
