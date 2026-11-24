"""`wf.on(event, handler)` calls the handler once each time the event fires, with its name and the run's context, and not for other events."""

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import IShaker
from orca.events.execution_context import CustomEventContext, ExecutionContext
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.sdk_features.kit import (
    build_world,
    close_world,
    new_lab,
    plate_template,
    run_to_end,
)

pytestmark = [pytest.mark.timeout(120)]


class _Calls:
    def __init__(self) -> None:
        self.seen: list[tuple[str, ExecutionContext]] = []

    def __call__(self, event: str, context: ExecutionContext) -> None:
        self.seen.append((event, context))


def test_add_event_handler_keeps_the_event_name_and_handler() -> None:
    template = WorkflowTemplate("hooks")
    handler = _Calls()

    WorkflowContext(template).on("CUSTOM.qc.EMITTED", handler)

    assert [(hook.event_name, hook.handler) for hook in template.event_hooks] == [("CUSTOM.qc.EMITTED", handler)]


@pytest.mark.asyncio
async def test_the_handler_is_called_once_per_firing_and_never_for_another_event() -> None:
    lab = new_lab()
    plate = plate_template()
    calls = _Calls()

    @orca.action(device=lab.shaker_1, inputs=[plate])
    async def report(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=100)
        await ctx.emit("qc", value="first")
        await ctx.emit("other", value="ignored")
        await ctx.emit("qc", value="second")

    @orca.method
    async def report_method(ctx: MethodContext):
        yield report

    @orca.thread(labware=plate, start="pad_1", end="pad_1")
    async def plate_journey(ctx: ThreadContext):
        yield report_method

    @orca.workflow(name="hooked")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)
        wf.on("CUSTOM.qc.EMITTED", calls)

    world = await build_world(lab, workflow)
    try:
        execution_id, ended = await run_to_end(world)
        assert set(ended.values()) == {"COMPLETED"}, ended

        assert [event for event, _ in calls.seen] == ["CUSTOM.qc.EMITTED", "CUSTOM.qc.EMITTED"]
        contexts = [context for _, context in calls.seen]
        assert all(isinstance(c, CustomEventContext) and c.execution_id == execution_id for c in contexts)
        assert [c.value for c in contexts if isinstance(c, CustomEventContext)] == ["first", "second"]
    finally:
        await close_world(world)
