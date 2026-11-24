"""Sample plates that each transfer into a shared receiver plate, at the well `ctx.pool_index` gives them."""

import asyncio
from dataclasses import dataclass, field

import orca.orca as orca
from orca.devices.device_interfaces import ILiquidHandler, IProtocolRunner, IShaker
from orca.resource_models.capacity import CapacityPolicy
from orca.resource_models.labware import CanContinueFn
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
from orca.spawn import DISPENSE
from orca.state.records import DeclaredTracking, DeclaredVolumeTransfer
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.labware_group import LabwareGroup, LabwareGroupMember
from orca.workflow_models.labware_state import IOverflowStrategy
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.sdk_features.kit import (
    PLATE_SITE,
    SECOND_PLATE_SITE,
    SHELVES,
    TIP_SITE,
    FeatureWorld,
    Lab,
    commands,
    plate_template,
    tips_template,
    tips_thread,
    wells_named,
)

TRANSFER_UL = 10.0
WELLS = ["A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1"]


@dataclass
class Transfers:
    """What each sample thread asked for and when, and each transfer in the order it ran."""

    arrivals: list[str] = field(default_factory=list)
    """The order the sample threads asked for the transfer method."""
    seen: list[tuple[str, str, int]] = field(default_factory=list)
    """(sample plate, receiver plate, pool index)."""


def capacity_workflow(
    lab: Lab,
    transfers: Transfers,
    *,
    capacity: CapacityPolicy | None = None,
    overflow_strategy: IOverflowStrategy | None = None,
    name: str = "pooling",
    hold_transfer: tuple[int, asyncio.Event] | None = None,
    on_either_handler: bool = False,
    receiver_shakes_when_full: bool = False,
    receiver_can_continue: CanContinueFn | None = None,
) -> WorkflowTemplate:
    """`hold_transfer=(n, event)` holds the n-th transfer (from 0) until `event` is set.

    `on_either_handler` runs the transfer as a closed protocol on the `handlers` pool, with no tips, so a
    contributor can reach a full receiver while another is still transferring into it.
    `receiver_shakes_when_full` gives each receiver a shake on `shaker_2` after its last contribution.
    """
    sample = plate_template("sample", 100.0)
    final = plate_template(
        "final", 0.0, group_sharing=GroupSharing.SHARED_ACROSS_GROUPS, submission_batching=SubmissionBatching.BATCHABLE,
        can_continue_fn=receiver_can_continue,
    )
    tips = tips_template(shared=True)

    async def wait_its_turn() -> None:
        if hold_transfer is not None and len(transfers.seen) == hold_transfer[0]:
            await hold_transfer[1].wait()

    @orca.action(
        device=lab.lh, inputs=[sample, final, tips],
        deck_positions={sample: PLATE_SITE, final: SECOND_PLATE_SITE, tips: TIP_SITE},
    )
    async def pipette(ctx: ActionContext) -> None:
        await wait_its_turn()
        index = ctx.pool_index("final")
        transfers.seen.append((ctx.labware("sample").name, ctx.labware("final").name, index))
        lh = ctx.device(ILiquidHandler)
        await lh.pick_up_tips(await ctx.next_tips("tips", 1))
        await lh.aspirate([ctx.plate("sample").well("A1")], [TRANSFER_UL])
        await lh.dispense([ctx.plate("final").well(WELLS[index])], [TRANSFER_UL])
        await lh.discard_tips()

    @orca.action(
        device=lab.pool("handlers") if on_either_handler else lab.lh, inputs=[sample, final],
        deck_positions={sample: PLATE_SITE, final: SECOND_PLATE_SITE},
        declares=DeclaredTracking(volume_transferred=[DeclaredVolumeTransfer(
            source="sample", target="final", volume_ul=TRANSFER_UL, source_wells=["A1"], target_wells=["A1"],
        )]),
    )
    async def run_a_transfer_protocol(ctx: ActionContext) -> None:
        await wait_its_turn()
        transfers.seen.append((ctx.labware("sample").name, ctx.labware("final").name, ctx.pool_index("final")))
        await ctx.device(IProtocolRunner).run_protocol("Transfer.hsl", {})

    transfer = run_a_transfer_protocol if on_either_handler else pipette

    @orca.method
    async def transfer_method(ctx: MethodContext):
        yield transfer

    @orca.thread(labware=sample, start=("stacker", DISPENSE), end="waste", contributes_to=["final"])
    async def sample_journey(ctx: ThreadContext):
        transfers.arrivals.append(ctx.labware.name)
        yield transfer_method

    @orca.action(device=lab.shaker_2, inputs=[final])
    async def shake_the_receiver(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=300)

    @orca.method
    async def finish_the_receiver(ctx: MethodContext):
        yield shake_the_receiver

    @orca.thread(labware=final, start=SHELVES[0], end=[*SHELVES[1:], "pad_1", "pad_2"])
    async def final_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[transfer_method])
        if receiver_shakes_when_full:
            yield finish_the_receiver

    tips_journey = tips_thread(tips, [transfer_method])

    @orca.workflow(name=name)
    def workflow(wf: WorkflowContext) -> None:
        wf.start(sample_journey)
        wf.thread(final_journey, capacity=capacity, overflow_strategy=overflow_strategy)
        if not on_either_handler:
            wf.thread(tips_journey)

    return workflow


def sample_group(number: int) -> LabwareGroup:
    return LabwareGroup(id=f"g{number}", members=(LabwareGroupMember("sample_journey"),))


def sample_groups(count: int) -> list[LabwareGroup]:
    return [sample_group(i) for i in range(1, count + 1)]


def transfers_by_receiver(world: FeatureWorld) -> dict[str, list[str]]:
    """Receiver plate name -> the sample plates that dispensed into it, in the order the handlers ran them."""
    handlers = [name for name in ("lh", "lh_2") if name in world.lab.drivers]
    pairs: list[tuple[float, str, str]] = []
    for handler in handlers:
        aspirates = commands(world, handler, "aspirate")
        dispenses = commands(world, handler, "dispense")
        for aspirate, dispense in zip(aspirates, dispenses, strict=True):
            pairs.append((dispense.started, wells_named(aspirate)[0][0], wells_named(dispense)[0][0]))
    by_receiver: dict[str, list[str]] = {}
    for _, sample, receiver in sorted(pairs):
        by_receiver.setdefault(receiver, []).append(sample)
    return by_receiver


def seen_by_receiver(transfers: Transfers) -> dict[str, list[str]]:
    """Receiver plate name -> the sample plates its transfer actions were handed, in the order they ran."""
    by_receiver: dict[str, list[str]] = {}
    for sample, receiver, _ in transfers.seen:
        by_receiver.setdefault(receiver, []).append(sample)
    return by_receiver
