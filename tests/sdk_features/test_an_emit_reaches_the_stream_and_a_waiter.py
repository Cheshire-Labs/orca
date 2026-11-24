"""`ctx.emit` puts `CUSTOM.<name>.EMITTED` with its value and data on the event stream, and a waiter in another thread gets both."""

import pytest
from pydantic import JsonValue

import orca.orca as orca
from orca.devices.device_interfaces import IShaker
from orca.events.execution_context import CustomEventContext
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.sdk_features.kit import (
    build_world,
    close_world,
    commands,
    events,
    new_lab,
    plate_template,
    run_to_end,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_an_emit_is_on_the_stream_and_reaches_a_waiter_with_its_data() -> None:
    lab = new_lab()
    sender, receiver = plate_template("sender"), plate_template("receiver")
    received: list[tuple[str | None, dict[str, JsonValue]]] = []

    @orca.action(device=lab.shaker_1, inputs=[sender])
    async def send(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=100)
        await ctx.emit("qc", value="pass", data={"cv": 3.5})

    @orca.action(device=lab.shaker_2, inputs=[receiver])
    async def receive(ctx: ActionContext) -> None:
        received.append(await ctx.wait_for("qc", timeout=30))
        await ctx.device(IShaker).shake(duration=1, speed=200)

    @orca.method
    async def send_method(ctx: MethodContext):
        yield send

    @orca.method
    async def receive_method(ctx: MethodContext):
        yield receive

    @orca.thread(labware=sender, start="pad_1", end="pad_1")
    async def sender_journey(ctx: ThreadContext):
        yield send_method

    @orca.thread(labware=receiver, start="pad_2", end="pad_2")
    async def receiver_journey(ctx: ThreadContext):
        yield receive_method

    @orca.workflow(name="emit_and_wait")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(sender_journey)
        wf.start(receiver_journey)

    world = await build_world(lab, workflow)
    try:
        execution_id, ended = await run_to_end(world)
        assert set(ended.values()) == {"COMPLETED"}, ended

        emitted = [e for e in events(world, "CUSTOM.qc.") if e.execution_id == execution_id]
        assert [e.event_name for e in emitted] == ["CUSTOM.qc.EMITTED"]
        context = emitted[0].context
        assert isinstance(context, CustomEventContext)
        assert (context.value, context.data) == ("pass", {"cv": 3.5})
        assert received == [("pass", {"cv": 3.5})]
        assert commands(world, "shaker_2")[0].started >= commands(world, "shaker_1")[0].started
    finally:
        await close_world(world)
