"""A standalone method gives the same device commands, plate record changes and operation records as the same method in a workflow."""

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import ILiquidHandler, IShaker
from orca.modes.run_modes import WorkflowRunMode
from orca.spawn import LEAVE_IN_PLACE
from orca.state.records import GenericOperationDetails
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.real_device_path import real_device_path
from tests.sdk_features.kit import (
    PLATE_SITE,
    TIP_SITE,
    TIPS_START,
    FeatureWorld,
    Lab,
    RecordedCommand,
    build_world,
    close_world,
    commands,
    new_lab,
    operations,
    plate_record,
    plate_template,
    run_to_end,
    tips_template,
    tips_thread,
    wait_for_the_end,
    wells_named,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

Work = tuple[list[tuple[str, str]], dict[str, float], list[tuple[str, str]]]
"""The commands sent, the plate's changed wells, and the saved operations (device, operation or command)."""
HOUSEKEEPING = frozenset({"open", "close", "park_gantry", "get_deck_state", "add_deck_labware", "remove_deck_labware"})


def _shake_then_take(lab: Lab) -> WorkflowTemplate:
    plate = plate_template()
    tips = tips_template()

    @orca.action(device=lab.shaker_1, inputs=[plate])
    async def shake(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=150)

    @orca.action(device=lab.lh, inputs=[plate, tips], deck_positions={plate: PLATE_SITE, tips: TIP_SITE})
    async def take(ctx: ActionContext) -> None:
        lh = ctx.device(ILiquidHandler)
        await lh.pick_up_tips(await ctx.next_tips("tips", 2))
        await lh.aspirate([ctx.plate("plate").well("A1"), ctx.plate("plate").well("B1")], [7.0, 3.0])
        await lh.discard_tips()

    @orca.method
    async def work_method(ctx: MethodContext):
        yield shake
        yield take

    @orca.thread(labware=plate, start="pad_1", end=("pad_1", LEAVE_IN_PLACE))
    async def plate_journey(ctx: ThreadContext):
        yield work_method

    tips_journey = tips_thread(tips, [work_method])

    @orca.workflow(name="shake_then_take")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)
        wf.thread(tips_journey)

    return workflow


def _what(command: RecordedCommand) -> str:
    """What a command did, without the instance names that differ between two worlds."""
    if command.method == "shake":
        return str(command.args["speed"])
    if command.method == "move_plate":
        return str((command.args["from_position"], command.args["to_position"]))
    if command.method == "aspirate":
        return str([w for _, w in wells_named(command)])
    picks = command.args.get("picks")
    if isinstance(picks, list):
        return str([p["positions"] for p in picks if isinstance(p, dict)])
    return ""


async def _work_done(world: FeatureWorld, execution_id: str) -> Work:
    sent = [(c.method, _what(c)) for device in ("shaker_1", "lh") for c in commands(world, device)]
    plates = [s for s in await world.runtime.labware.list_all() if s.template_name == "plate"]
    [plate] = plates
    volumes = {w: v for w, v in (await plate_record(world, plate.id)).volumes.items() if v != 100.0}
    ops = [
        (op.device_name, op.operation.value if not isinstance(op.details, GenericOperationDetails) else op.details.command)
        for record in await operations(world, execution_id) for op in record.operations
        if op.device_name in ("shaker_1", "lh")
    ]
    return sent, volumes, ops


async def test_the_standalone_run_does_the_same_work_as_the_workflow_run() -> None:
    in_a_workflow, standalone = await in_a_workflow_and_standalone()

    assert standalone[:2] == in_a_workflow[:2]
    assert _device_work(standalone[2]) == _device_work(in_a_workflow[2])
    assert in_a_workflow[1] == {"A1": 93.0, "B1": 97.0}
    assert [m for m, _ in in_a_workflow[0]] == [
        "shake", "move_plate", "pick_up_tips", "aspirate", "discard_tips", "move_plate",
    ]


def _device_work(ops: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The saved operations without the opening, closing, parking and deck bookkeeping around them."""
    return [op for op in ops if op[1] not in HOUSEKEEPING]


async def in_a_workflow_and_standalone() -> tuple[Work, Work]:
    lab = new_lab()
    world = await build_world(lab, _shake_then_take(lab))
    try:
        execution_id, ended = await run_to_end(world)
        assert set(ended.values()) == {"COMPLETED"}, ended
        in_a_workflow = await _work_done(world, execution_id)
    finally:
        await close_world(world)
    async with real_device_path():
        return in_a_workflow, await _standalone_run()


async def _standalone_run() -> Work:
    """The method run on its own, in a world with its own device path, so its drivers start fresh."""
    lab = new_lab()
    world = await build_world(lab, _shake_then_take(lab))
    try:
        record = await world.runtime.submit_method(
            "shake_then_take", "work_method",
            labware_start={"plate": "pad_1", "tips": TIPS_START}, labware_end={"plate": "pad_1", "tips": TIPS_START},
            mode=WorkflowRunMode.CHECK_RUN,
        )
        ended = await wait_for_the_end(world, record.id)
        assert set(ended.values()) == {"COMPLETED"}, ended
        return await _work_done(world, record.id)
    finally:
        await close_world(world)
