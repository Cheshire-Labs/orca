"""Workflow definition for the PLR example.

The workflow describes what happens to each piece of labware as it moves
through the system: which actions run, on which devices, in what order,
and how threads join each other.

Pattern:
  * Labware templates are stateless -- declared at module scope.
  * ``build_workflow(topology)`` is a builder that looks devices up from
    the topology and closes over them inside @orca.action decorators.
"""

import csv
import logging
import os
from dataclasses import dataclass
from typing import List, cast

import orca.orca as orca

from orca.spawn import DISPENSE, LEAVE_IN_PLACE, REUSE_EXISTING
from orca.devices.device_interfaces import ILiquidHandler
from orca.devices.devices import LiquidHandler, Reader, Waste
from orca.devices.sealer import Sealer
from orca.events.execution_context import LocationActionExecutionContext
from orca.sdk.build import Topology
from orca.sdk.events import ExecutionContext, SystemBoundEventHandler
from orca.sdk.labware import AnyLabwareTemplate, PlateTemplate, TipRackTemplate, TroughTemplate
from orca.variables.variable_definition import VariableDefinition
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.status_enums import FailurePolicy
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate

orca_logger = logging.getLogger("orca")


# --- Cherry pick worklist loader ---

@dataclass(frozen=True)
class CherryPickTransfer:
    src_well: str
    dest_well: str
    volume: float


def load_worklist(path: str) -> List[CherryPickTransfer]:
    with open(path, newline="") as f:
        reader_csv = csv.DictReader(f)
        return [
            CherryPickTransfer(
                src_well=row["src_well"],
                dest_well=row["dest_well"],
                volume=float(row["volume"]),
            )
            for row in reader_csv
        ]


WORKLIST_PATH = os.path.join(os.path.dirname(__file__), "cherry_pick_worklist.csv")


# --- Labware templates (stateless, safe to share across builds) ---

sample_plate = PlateTemplate("sample_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
dest_plate = PlateTemplate("dest_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
tips = TipRackTemplate("tips", labware_type="hamilton_96_tiprack_10uL_filter", with_tips=True)
reagent_trough = TroughTemplate("reagent_trough", labware_type="hamilton_1_trough_200ml_Vb")

# Deck sites on the liquid_handler carriers (declared in topology.py). The plate
# handoff is carrier-7-2; TROUGH_SITE is the device-qualified form a thread needs.
SAMPLE_PLATE_SITE = "carrier-7-0"
DEST_PLATE_SITE = "carrier-7-1"
TIPS_SITE = "carrier-15-0"
TROUGH_SITE = "liquid_handler/carrier-25-0"


def build_workflow(topology: Topology) -> WorkflowTemplate:
    """Build the PLR example workflow bound to the given topology."""

    liquid_handler = topology.device("liquid_handler", LiquidHandler)
    reader = topology.device("reader", Reader)
    sealer = topology.device("sealer", Sealer)
    waste = topology.device("waste", Waste)
    shaker_pool = topology.pool("shaker_pool")

    # --- Actions ---

    @orca.action(
        device=liquid_handler,
        inputs=[sample_plate, dest_plate, tips],
        deck_positions={
            sample_plate: SAMPLE_PLATE_SITE,
            dest_plate: DEST_PLATE_SITE,
            tips: TIPS_SITE,
        },
    )
    async def cherry_pick(ctx: ActionContext):
        lh = ctx.device(ILiquidHandler)
        src = ctx.plate("sample_plate")
        dst = ctx.plate("dest_plate")
        rack = ctx.tip_rack("tips")
        worklist = load_worklist(WORKLIST_PATH)

        await lh.pick_up_tips([rack.tip_spot("A1")])
        for transfer in worklist:
            await lh.aspirate(
                [src.well(transfer.src_well)], [transfer.volume],
                flow_rates=[10.0], offsets_z=[1.0],
            )
            await lh.dispense(
                [dst.well(transfer.dest_well)], [transfer.volume],
                flow_rates=[15.0], offsets_z=[0.5],
            )
        await lh.drop_tips([rack.tip_spot("A1")])

    @orca.action(
        device=liquid_handler,
        inputs=[dest_plate, reagent_trough, tips],
        deck_positions={dest_plate: DEST_PLATE_SITE, tips: TIPS_SITE},
    )
    async def serial_dilute(ctx: ActionContext):
        lh = ctx.device(ILiquidHandler)
        plate = ctx.plate("dest_plate")
        rack = ctx.tip_rack("tips")
        factor = await ctx.param("dilution_factor", float)

        diluent_vol = 90.0
        transfer_vol = diluent_vol / (factor - 1)

        await lh.pick_up_tips([rack.tip_spot("C1")])
        for row in ["B2", "B3", "B4"]:
            await lh.aspirate([plate.well("A1")], [diluent_vol], flow_rates=[50.0])
            await lh.dispense([plate.well(row)], [diluent_vol], flow_rates=[50.0])
        await lh.drop_tips([rack.tip_spot("C1")])

        dilution_pairs = [("B1", "B2"), ("B2", "B3"), ("B3", "B4")]
        tip_spots = ["D1", "E1", "F1"]
        for (src_well, dst_well), tip_id in zip(dilution_pairs, tip_spots):
            await lh.pick_up_tips([rack.tip_spot(tip_id)])
            await lh.aspirate(
                [plate.well(src_well)], [transfer_vol],
                flow_rates=[20.0], offsets_z=[2.0],
            )
            await lh.dispense(
                [plate.well(dst_well)], [transfer_vol],
                flow_rates=[20.0], offsets_z=[0.5],
            )
            await lh.drop_tips([rack.tip_spot(tip_id)])

    @orca.action(device=shaker_pool, inputs=[dest_plate])
    async def shake(ctx: ActionContext):
        await ctx.device().shake(duration=60, speed=800)

    @orca.action(device=reader, inputs=[dest_plate])
    async def read_plate(ctx: ActionContext):
        await ctx.device().read("absorbance.pro", "/dev/null")
        await ctx.emit(
            "plate_reading",
            value="complete",
            data={"absorbance": [0.5, 0.8, 0.3, 1.2, 0.6, 0.9, 0.4, 0.7]},
        )

    @orca.action(
        device=liquid_handler,
        inputs=[dest_plate],
        deck_positions={dest_plate: DEST_PLATE_SITE},
    )
    async def evaluate_qc(ctx: ActionContext):
        _, data = await ctx.wait_for("plate_reading")
        absorbance = cast(list[float], data["absorbance"])
        avg = sum(absorbance) / len(absorbance)
        result = "pass" if avg < 1.0 else "fail"
        orca_logger.info("QC result: %s (avg absorbance %.3f)", result, avg)
        await ctx.emit("qc_result", value=result, data={"avg": avg})

    @orca.action(
        device=sealer,
        inputs=[dest_plate],
        failure_policy=FailurePolicy.PAUSE,
        tag="seal",
    )
    async def seal(ctx: ActionContext):
        await ctx.device().seal(temperature=100, duration=60)

    @orca.action(device=waste, inputs=[AnyLabwareTemplate()])
    async def discard(ctx: ActionContext):
        orca_logger.info("Discarding labware to waste.")

    # --- Methods ---

    @orca.method
    async def cherry_pick_step(ctx: MethodContext):
        yield cherry_pick

    @orca.method
    async def dilute_step(ctx: MethodContext):
        yield serial_dilute

    @orca.method
    async def shake_step(ctx: MethodContext):
        yield shake

    @orca.method
    async def read_step(ctx: MethodContext):
        yield read_plate

    @orca.method
    async def evaluate_qc_step(ctx: MethodContext):
        yield evaluate_qc

    @orca.method(failure_policy=FailurePolicy.PAUSE)
    async def seal_step(ctx: MethodContext):
        yield seal

    # --- Threads ---

    @orca.thread(labware=sample_plate, start=("stacker", DISPENSE), end="waste")
    async def source_plate_journey(ctx: ThreadContext):
        yield orca.join(allows=[cherry_pick_step])

    @orca.thread(labware=dest_plate, start=("stacker", DISPENSE), end="waste")
    async def dest_plate_journey(ctx: ThreadContext):
        yield cherry_pick_step
        yield dilute_step
        yield shake_step
        yield read_step
        yield evaluate_qc_step
        yield orca.branch("qc_result", {
            "pass": [seal_step],
            "fail": [dilute_step, read_step, evaluate_qc_step],
        })

    @orca.thread(labware=tips, start=("stacker", DISPENSE), end="waste")
    async def tips_journey(ctx: ThreadContext):
        yield orca.join(allows=[cherry_pick_step, dilute_step])

    # RESIDENT reagent: start == end == its trough-carrier deck site, so it stays
    # put and materializes on the driver deck via ledger reconcile, not routing.
    @orca.thread(
        labware=reagent_trough,
        start=(TROUGH_SITE, REUSE_EXISTING),
        end=(TROUGH_SITE, LEAVE_IN_PLACE),
    )
    async def trough_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[dilute_step])

    # --- Event handler ---

    class QCLogger(SystemBoundEventHandler):
        def handle(self, event: str, context: ExecutionContext) -> None:
            if isinstance(context, LocationActionExecutionContext):
                name = context.action_name
                if name and "evaluate_qc" in name:
                    orca_logger.info(
                        "QC evaluation observed by handler: action=%s status=%s",
                        name,
                        context.action_status,
                    )

    # --- Workflow ---

    @orca.workflow(name="learn_orca")
    def learn_orca_workflow(wf: WorkflowContext):
        wf.variable(
            "dilution_factor",
            VariableDefinition(
                type="float",
                default=10.0,
                min=2.0,
                max=100.0,
                description="Serial dilution factor",
                unit="x",
            ),
        )
        wf.start(dest_plate_journey)
        wf.thread(source_plate_journey)
        wf.thread(tips_journey)
        wf.thread(trough_journey)
        wf.on("ACTION.COMPLETED", QCLogger())

    return learn_orca_workflow
