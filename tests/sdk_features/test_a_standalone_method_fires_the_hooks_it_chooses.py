"""A standalone method run fires the workflow's `wf.on` handlers its `event_hooks` choose: all when omitted, none for
`[]`, only those listed otherwise; a hook the workflow has no handler for is refused."""

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import IShaker
from orca.events.execution_context import ExecutionContext
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.errors import EventHooksNotInTheWorkflowError
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.sdk_features.kit import Lab, build_world, close_world, new_lab, plate_template, wait_for_the_end

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

PING = "CUSTOM.ping.EMITTED"
PONG = "CUSTOM.pong.EMITTED"


def _pinging(lab: Lab, called: list[str]) -> WorkflowTemplate:
    plate = plate_template()

    @orca.action(device=lab.shaker_1, inputs=[plate])
    async def emit_both(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=100)
        await ctx.emit("ping")
        await ctx.emit("pong")

    @orca.method
    async def emit_method(ctx: MethodContext):
        yield emit_both

    @orca.thread(labware=plate, start="pad_1", end="pad_1")
    async def plate_journey(ctx: ThreadContext):
        yield emit_method

    @orca.workflow(name="hooks")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)
        wf.on(PING, lambda event, _context: called.append(event))
        wf.on(PONG, lambda event, _context: called.append(event))

    return workflow


@pytest.mark.parametrize(("hooks", "fired"), [(None, [PING, PONG]), ([], []), ([PONG], [PONG])])
async def test_the_handlers_its_hooks_choose_are_called(hooks: list[str] | None, fired: list[str]) -> None:
    lab = new_lab()
    called: list[str] = []
    world = await build_world(lab, _pinging(lab, called))
    try:
        record = await world.runtime.submit_method(
            "hooks", "emit_method", {"plate": "pad_1"}, {"plate": "pad_1"}, mode=WorkflowRunMode.SIM, event_hooks=hooks,
        )
        ended = await wait_for_the_end(world, record.id)

        assert set(ended.values()) == {"COMPLETED"}, ended
        assert called == fired
    finally:
        await close_world(world)


async def test_a_hook_the_workflow_has_no_handler_for_is_refused() -> None:
    lab = new_lab()
    called: list[str] = []
    world = await build_world(lab, _pinging(lab, called))
    try:
        with pytest.raises(EventHooksNotInTheWorkflowError, match="CUSTOM.nothing.EMITTED"):
            await world.runtime.submit_method(
                "hooks", "emit_method", {"plate": "pad_1"}, {"plate": "pad_1"},
                mode=WorkflowRunMode.SIM, event_hooks=["CUSTOM.nothing.EMITTED"],
            )
        assert called == []
    finally:
        await close_world(world)
