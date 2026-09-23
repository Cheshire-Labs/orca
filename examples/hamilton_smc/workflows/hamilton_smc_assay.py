"""Workflow definition for the Hamilton SMC (Single Molecule Counting) assay.

Models a representative bead-capture IL-6 immunoassay on two Hamilton ML STARs.
Every liquid-handler step is an 8-channel transfer looped across all 12 columns
of the 96-well plate.

The assay plate arrives PRE-LOADED: standard curve in triplicate (rows A-C) and
samples in duplicate (rows D-H) are prepared manually upstream, so the workflow
never models the serial dilution. Reagents arrive already prepared in their
troughs.

  Target Capture     75 uL sample then 100 uL capture beads per well, 2 h capture.
  Post-Capture Wash  magnetic wash.
  Detection          20 uL detection antibody per well, 1 h.
  Post-Detection     4-cycle wash, 90 s shake, final aspiration.
  Elution            10 uL Elution Buffer B per well, 10 min.
  Neutralize         10 uL Buffer D pre-dispensed into a fresh plate; 10 uL eluate
                     transferred onto it.
  Read               20 uL neutralized eluate to the 384-well SMC read plate. Up to
                     four 96-well assay plates fill its four interleaved quadrants,
                     each contributor routed to its own quadrant by contribution
                     index (0=A1, 1=A2, 2=B1, 3=B2).

Reagents are deck-resident troughs, declared as replenished sources: beads and
detection on mlstar_1, elution Buffer B and Buffer D on mlstar_2. A PLR Trough is one
undivided pool, so an 8-channel draw dips every channel into the single container.
``replenished`` keeps each trough non-depleting in sim, so a shared reagent never runs
dry however many plates draw from it (unbounded batch). On real hardware the trough
holds the actual finite reagent, which the operator sizes or refills for the batch.

Tips: one rack per pipetting action (a full-plate fresh-tips transfer consumes an
entire 96-tip rack; the engine reuses one rack instance until depleted, so shared
racks would not survive a full plate). 300 uL tips for the >=20 uL steps (sample,
beads, detection, read), 50 uL tips for the 10 uL steps (Buffer B, Buffer D,
eluate). Sample-to-sample transfers change tips every column; a single reagent
drawn from a trough reuses one 8-tip set across the plate.

All reagent labware stays stationary on the ML STAR decks across executions via
``REUSE_EXISTING`` + ``LEAVE_IN_PLACE``.

Buffer D pre-load and the eluate transfer both land in the neutralization plate at
mlstar_2, so they are one action driven by the assay plate (which already holds
mlstar_2 from eluting); the fresh neutralization plate stays a passive receiver
that never reserves the device, which a second driving thread would deadlock
against.

Submission shape (same as the Bravo SMC example):
  * ``final_plate`` is ``SHARED_ACROSS_GROUPS + BATCHABLE``.
  * Single-group + STANDALONE produces one fresh final plate per submission.
  * Multi-group / JOIN_EXISTING coalesces samples into a shared final plate.
"""

import orca.orca as orca

from cheshire_drivers.labware_interfaces import IPlate, ITipRack, ITrough
from cheshire_drivers.pipetting import MixParams, PipettingProfile
from orca.resource_models.capacity import CapacityPolicy, OverflowAction
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, REUSE_EXISTING
from orca.devices.centrifuge import Centrifuge
from orca.devices.device_interfaces import ILiquidHandler
from orca.devices.devices import Delidder, LiquidHandler, PlateWasher, Reader
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
from orca.sdk.build import Topology
from orca.sdk.labware import (
    AnyLabwareTemplate,
    LabwareInitialState,
    LabwareTemplate,
    PlateTemplate,
    TipRackTemplate,
    TroughTemplate,
)
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate


# --- Labware templates (stateless, safe to share across builds) ---

sample_plate = PlateTemplate("sample_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
plate_1 = PlateTemplate("plate_1", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
neut_plate = PlateTemplate("neut_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
final_plate = PlateTemplate(
    "final_plate",
    labware_type="BioRad_384_wellplate_50uL_Vb",
    group_sharing=GroupSharing.SHARED_ACROSS_GROUPS,
    submission_batching=SubmissionBatching.BATCHABLE,
)

# Deck-resident reagent troughs as replenished sources: seeded full and non-depleting
# in sim, so a shared reagent never runs dry however many plates draw from it.
_REAGENT = LabwareInitialState(max_fill=True, replenished=True)
bead_reservoir = TroughTemplate(
    "bead_reservoir", labware_type="hamilton_1_trough_200ml_Vb", initial_state=_REAGENT,
)
detection_reservoir = TroughTemplate(
    "detection_reservoir", labware_type="hamilton_1_trough_200ml_Vb", initial_state=_REAGENT,
)
buffer_b_reservoir = TroughTemplate(
    "buffer_b_reservoir", labware_type="hamilton_1_trough_200ml_Vb", initial_state=_REAGENT,
)
buffer_d_reservoir = TroughTemplate(
    "buffer_d_reservoir", labware_type="hamilton_1_trough_200ml_Vb", initial_state=_REAGENT,
)

reservoirs: list[LabwareTemplate] = [
    bead_reservoir, detection_reservoir, buffer_b_reservoir, buffer_d_reservoir,
]

# One tip rack per pipetting action. 300 uL tips for >=20 uL, 50 uL for the 10 uL
# steps. A fresh-tips-per-column transfer uses a whole rack, so no rack is shared.
tips_sample = TipRackTemplate("tips_sample", labware_type="hamilton_96_tiprack_300uL_filter", with_tips=True)
tips_beads = TipRackTemplate("tips_beads", labware_type="hamilton_96_tiprack_300uL_filter", with_tips=True)
tips_detection = TipRackTemplate("tips_detection", labware_type="hamilton_96_tiprack_300uL_filter", with_tips=True)
tips_read = TipRackTemplate("tips_read", labware_type="hamilton_96_tiprack_300uL_filter", with_tips=True)
tips_buffer_b = TipRackTemplate("tips_buffer_b", labware_type="hamilton_96_tiprack_50uL_filter", with_tips=True)
tips_buffer_d = TipRackTemplate("tips_buffer_d", labware_type="hamilton_96_tiprack_50uL_filter", with_tips=True)
tips_eluate = TipRackTemplate("tips_eluate", labware_type="hamilton_96_tiprack_50uL_filter", with_tips=True)


_ROWS = "ABCDEFGH"
_COLUMNS = tuple(range(1, 13))
# Reverse-pipetting prime: extra volume aspirated once and discarded with the tip.
_REVERSE_EXCESS = 5.0


def _column(plate: IPlate, col: int) -> list:
    """The 8 wells (rows A-H) of one plate column."""
    return [plate.well(f"{row}{col}") for row in _ROWS]


def _tip_column(rack: ITipRack, col: int) -> list:
    """The 8 tip spots (rows A-H) of one tip-rack column."""
    return [rack.tip_spot(f"{row}{col}") for row in _ROWS]


def _quadrant_column(dst_384: IPlate, col: int, quadrant: int) -> list:
    """The 8 read-plate wells for column ``col`` of a 96-well contribution, written
    into one of the 384 plate's four interleaved quadrants. 96 (row i, col j) maps to
    384 (2i + row_offset, 2(j-1) + 1 + col_offset); quadrant 0=A1, 1=A2, 2=B1, 3=B2."""
    row_offset, col_offset = quadrant // 2, quadrant % 2
    return [
        dst_384.well(f"{chr(ord('A') + 2 * i + row_offset)}{2 * col - 1 + col_offset}")
        for i in range(8)
    ]


async def _transfer_columns(
    lh: ILiquidHandler, src: IPlate, dst: IPlate, rack: ITipRack,
    volume: float, flow_rate: float,
) -> None:
    """Well-to-well transfer across all 12 columns with FRESH tips per column, so
    distinct samples never share tips. Consumes the whole 96-tip rack."""
    for col in _COLUMNS:
        await lh.pick_up_tips(_tip_column(rack, col))
        await lh.aspirate(_column(src, col), [volume] * 8, flow_rates=[flow_rate] * 8)
        await lh.dispense(_column(dst, col), [volume] * 8, flow_rates=[flow_rate] * 8)
        await lh.discard_tips()


async def _dispense_reagent(
    lh: ILiquidHandler, trough: ITrough, dst: IPlate, rack: ITipRack,
    volume: float, flow_rate: float, technique: PipettingProfile | None = None,
    reverse: bool = False,
) -> None:
    """One trough reagent into all 96 wells, reusing a single 8-tip set across the
    plate (a multichannel drawing from one reservoir)."""
    await lh.pick_up_tips(_tip_column(rack, 1))
    for i, col in enumerate(_COLUMNS):
        # Reverse pipetting (Buffer B/D onto the bead pellet): over-aspirate once and
        # never blow out, so every dispense is a clean volume and the surplus is tossed.
        extra = _REVERSE_EXCESS if (reverse and i == 0) else 0.0
        await lh.aspirate(trough, [volume + extra] * 8, flow_rates=[flow_rate] * 8, technique=technique)
        await lh.dispense(_column(dst, col), [volume] * 8, flow_rates=[flow_rate] * 8)
    await lh.discard_tips()


async def _transfer_to_read(
    lh: ILiquidHandler, src: IPlate, dst_384: IPlate, rack: ITipRack,
    volume: float, flow_rate: float, quadrant: int,
) -> None:
    """Neutralized eluate (96 wells) into one interleaved quadrant of the 384 read
    plate, fresh tips per column."""
    for col in _COLUMNS:
        await lh.pick_up_tips(_tip_column(rack, col))
        await lh.aspirate(_column(src, col), [volume] * 8, flow_rates=[flow_rate] * 8)
        await lh.dispense(_quadrant_column(dst_384, col, quadrant), [volume] * 8, flow_rates=[flow_rate] * 8)
        await lh.discard_tips()


def build_workflow(topology: Topology) -> WorkflowTemplate:
    """Build the Hamilton SMC (IL-6) assay workflow bound to the given topology."""

    biotek_1 = topology.device("biotek_1", PlateWasher)
    biotek_2 = topology.device("biotek_2", PlateWasher)
    mlstar_1 = topology.device("mlstar_1", LiquidHandler)
    mlstar_2 = topology.device("mlstar_2", LiquidHandler)
    centrifuge_device = topology.device("centrifuge", Centrifuge)
    delidder = topology.device("delidder", Delidder)
    smc_pro = topology.device("smc_pro", Reader)
    shaker_collection = topology.pool("shaker_collection")

    # --- Actions ---

    @orca.action(
        device=mlstar_1,
        inputs=[sample_plate, bead_reservoir, tips_sample, tips_beads, plate_1],
        deck_positions={
            sample_plate: "carrier-7-0",
            plate_1: "carrier-7-1",
            tips_sample: "carrier-15-0",
            tips_beads: "carrier-15-2",
        },
    )
    async def run_target_capture(ctx: ActionContext):
        """Add 75 uL sample and 100 uL coated beads to the assay plate."""
        lh = ctx.device(ILiquidHandler)
        # Step 1: 75 uL Standards/Samples into the assay plate, fresh tips/col.
        await _transfer_columns(
            lh, ctx.plate("sample_plate"), ctx.plate("plate_1"),
            ctx.tip_rack("tips_sample"), volume=75.0, flow_rate=100.0,
        )
        # Step 3: 100 uL Coated Beads per well, resuspended, reused tips.
        await _dispense_reagent(
            lh, ctx.trough("bead_reservoir"), ctx.plate("plate_1"),
            ctx.tip_rack("tips_beads"), volume=100.0, flow_rate=100.0,
            technique=PipettingProfile(
                mix=MixParams(volume=80.0, repetitions=5, flow_rate=150.0)
            ),
        )

    @orca.action(device=shaker_collection, inputs=[plate_1])
    async def shake_2hrs(ctx: ActionContext):
        """2 h capture shake at Jitterbug setting #4 (~875 rpm)."""
        await ctx.device().shake(duration=7200, speed=875)

    @orca.action(device=centrifuge_device, inputs=[plate_1])
    async def run_post_capture_spin(ctx: ActionContext):
        """Spin the assay plate at 1,100 x g for 1 min after capture, before wash."""
        await ctx.device().centrifuge(g=1100, duration=60)

    @orca.action(device=biotek_1, inputs=[plate_1])
    async def run_post_capture_wash(ctx: ActionContext):
        """Wash the assay plate after bead capture."""
        await ctx.device().run_protocol("post_capture_wash.pro", {})

    @orca.action(
        device=mlstar_1, inputs=[detection_reservoir, plate_1, tips_detection],
        deck_positions={
            plate_1: "carrier-7-1",
            # Own slot (not 15-0, which tips_sample uses in target_capture): in a
            # batch, one submission's tips_sample can still hold 15-0 when another's
            # tips_detection needs it, blocking convergence.
            tips_detection: "carrier-15-3",
        },
    )
    async def run_add_detection_antibody(ctx: ActionContext):
        """Add 20 uL detection antibody to every well."""
        lh = ctx.device(ILiquidHandler)
        # 20 uL Detection Antibody per well (reagent, reused tips).
        await _dispense_reagent(
            lh, ctx.trough("detection_reservoir"), ctx.plate("plate_1"),
            ctx.tip_rack("tips_detection"), volume=20.0, flow_rate=100.0,
        )

    @orca.action(device=shaker_collection, inputs=[plate_1])
    async def shake_1hr(ctx: ActionContext):
        """1 h detection shake at Jitterbug setting #5 (~1000 rpm)."""
        await ctx.device().shake(duration=3600, speed=1000)

    @orca.action(device=biotek_2, inputs=[plate_1])
    async def run_post_detection_wash(ctx: ActionContext):
        """Wash the assay plate after the detection incubation."""
        await ctx.device().run_protocol("post_detection_wash.pro", {})

    @orca.action(device=shaker_collection, inputs=[plate_1])
    async def shake_90s(ctx: ActionContext):
        """90 s post-detection shake at Jitterbug setting #3 (~750 rpm)."""
        await ctx.device().shake(duration=90, speed=750)

    @orca.action(device=biotek_2, inputs=[plate_1])
    async def run_final_aspiration(ctx: ActionContext):
        """Aspirate the wells dry before elution."""
        await ctx.device().run_protocol("final_aspiration.pro", {})

    @orca.action(
        device=mlstar_2, inputs=[buffer_b_reservoir, plate_1, tips_buffer_b],
        deck_positions={
            plate_1: "carrier-7-0",
            tips_buffer_b: "carrier-15-0",
        },
    )
    async def run_add_elution_buffer_b(ctx: ActionContext):
        """Add 10 uL elution buffer B to every well."""
        lh = ctx.device(ILiquidHandler)
        # 10 uL Elution Buffer B per well (reagent, reused tips).
        await _dispense_reagent(
            lh, ctx.trough("buffer_b_reservoir"), ctx.plate("plate_1"),
            ctx.tip_rack("tips_buffer_b"), volume=10.0, flow_rate=80.0, reverse=True,
        )

    @orca.action(device=shaker_collection, inputs=[plate_1])
    async def shake_10min(ctx: ActionContext):
        """10 min elution shake at Jitterbug setting #5 (~1000 rpm)."""
        await ctx.device().shake(duration=600, speed=1000)

    # With no standalone magnet device, the biotek washer's magnet holds the plate.
    @orca.action(device=biotek_2, inputs=[plate_1])
    async def run_magnetic_pellet(ctx: ActionContext):
        """Pellet the beads on the magnet for 2 min so the eluate transfers bead-free."""
        await ctx.device().run_protocol("magnetic_pellet.pro", {})

    # Driven by the assay plate (already on mlstar_2 from eluting); the neutralization
    # plate joins as a passive destination. Buffer D reuses tips; the eluate changes
    # tips per column.
    @orca.action(
        device=mlstar_2,
        inputs=[plate_1, neut_plate, buffer_d_reservoir, tips_buffer_d, tips_eluate],
        deck_positions={
            plate_1: "carrier-7-0",
            neut_plate: "carrier-7-1",
            # Each mlstar_2 tip rack gets its own carrier-15 site so batched
            # submissions never block each other on a shared slot: 15-0 tips_buffer_b,
            # 15-2 tips_eluate, 15-3 tips_buffer_d, 15-4 tips_read (15-1 is the handoff).
            tips_buffer_d: "carrier-15-3",
            tips_eluate: "carrier-15-2",
        },
    )
    async def run_neutralize_and_transfer(ctx: ActionContext):
        """Pre-dispense 10 uL Buffer D, then transfer 10 uL eluate onto it."""
        lh = ctx.device(ILiquidHandler)
        # Pre-load 10 uL Buffer D into the neutralization plate (reagent, reused tips).
        await _dispense_reagent(
            lh, ctx.trough("buffer_d_reservoir"), ctx.plate("neut_plate"),
            ctx.tip_rack("tips_buffer_d"), volume=10.0, flow_rate=60.0, reverse=True,
        )
        # Transfer 10 uL eluate onto the Buffer D (neutralizes), fresh tips/col.
        await _transfer_columns(
            lh, ctx.plate("plate_1"), ctx.plate("neut_plate"),
            ctx.tip_rack("tips_eluate"), volume=10.0, flow_rate=40.0,
        )

    @orca.action(device=shaker_collection, inputs=[neut_plate])
    async def shake_neutralize(ctx: ActionContext):
        """2 min neutralization mix at Jitterbug setting #5 (~1000 rpm)."""
        await ctx.device().shake(duration=120, speed=1000)

    @orca.action(device=centrifuge_device, inputs=[neut_plate])
    async def spin_neut(ctx: ActionContext):
        """Spin the neutralization plate at 1,100 x g for 1 min."""
        await ctx.device().centrifuge(g=1100, duration=60)

    # The neutralization plate drives; final_plate joins. Each contributor writes its
    # own interleaved quadrant, keyed by ctx.pool_index.
    @orca.action(
        device=mlstar_2, inputs=[neut_plate, final_plate, tips_read],
        deck_positions={
            neut_plate: "carrier-7-1",
            # The 384 read plate is a long-lived receiver: it camps on the ML STAR
            # across every contribution, so it gets its own site (carrier-7-3) rather
            # than plate_1's carrier-7-0, which the assay plates need for elution.
            final_plate: "carrier-7-3",
            tips_read: "carrier-15-4",
        },
    )
    async def run_transfer_to_read_plate(ctx: ActionContext):
        """Transfer 20 uL neutralized eluate into this batch's quadrant of the 384 read plate."""
        lh = ctx.device(ILiquidHandler)
        # The engine stamps this contribution's 0-based index onto the shared read
        # plate; four assay plates fill quadrants 0-3 of one 384.
        quadrant = ctx.pool_index("final_plate")
        if quadrant > 3:
            raise ValueError(
                f"final_plate has four quadrants but received contribution "
                f"{quadrant}; cap the batch at four assay plates per 384 read plate."
            )
        await _transfer_to_read(
            lh, ctx.plate("neut_plate"), ctx.plate("final_plate"),
            ctx.tip_rack("tips_read"), volume=20.0, flow_rate=60.0, quadrant=quadrant,
        )

    @orca.action(device=centrifuge_device, inputs=[final_plate])
    async def spin_plate(ctx: ActionContext):
        """Spin the 384 read plate at 1,100 x g for 1 min before the read."""
        await ctx.device().centrifuge(g=1100, duration=60)

    @orca.action(device=smc_pro, inputs=[final_plate])
    async def read_plate(ctx: ActionContext):
        """Read the 384 plate on the SMC Pro and write results.csv."""
        await ctx.device().read("read.pro", "results.csv")

    @orca.action(device=delidder, inputs=[AnyLabwareTemplate()])
    async def delid_plate(ctx: ActionContext):
        """Take the lid off whatever labware is presented."""
        await ctx.device().delid()

    # --- Methods ---

    @orca.method
    async def target_capture(ctx: MethodContext):
        yield run_target_capture

    @orca.method
    async def incubate_2hrs(ctx: MethodContext):
        yield shake_2hrs

    @orca.method
    async def post_capture_spin(ctx: MethodContext):
        yield run_post_capture_spin

    @orca.method
    async def post_capture_wash(ctx: MethodContext):
        yield run_post_capture_wash

    @orca.method
    async def add_detection_antibody(ctx: MethodContext):
        yield run_add_detection_antibody

    @orca.method
    async def incubate_1hr(ctx: MethodContext):
        yield shake_1hr

    @orca.method
    async def post_detection_wash(ctx: MethodContext):
        yield run_post_detection_wash

    @orca.method
    async def post_detection_shake(ctx: MethodContext):
        yield shake_90s

    @orca.method
    async def final_aspiration(ctx: MethodContext):
        yield run_final_aspiration

    @orca.method
    async def add_elution_buffer_b(ctx: MethodContext):
        yield run_add_elution_buffer_b

    @orca.method
    async def incubate_10min(ctx: MethodContext):
        yield shake_10min

    @orca.method
    async def magnetic_pellet(ctx: MethodContext):
        yield run_magnetic_pellet

    @orca.method
    async def neutralize_and_transfer(ctx: MethodContext):
        yield run_neutralize_and_transfer

    @orca.method
    async def neutralize_shake(ctx: MethodContext):
        yield shake_neutralize

    @orca.method
    async def centrifuge_neut(ctx: MethodContext):
        yield spin_neut

    @orca.method
    async def transfer_to_read_plate(ctx: MethodContext):
        yield run_transfer_to_read_plate

    @orca.method
    async def centrifuge(ctx: MethodContext):
        yield spin_plate

    @orca.method
    async def read(ctx: MethodContext):
        yield read_plate

    @orca.method
    async def delid(ctx: MethodContext):
        yield delid_plate

    # --- Threads ---

    @orca.thread(
        labware=plate_1,
        start=("stacker_3", DISPENSE),
        end="waste_1",
        contributes_to=["neut_plate"],
    )
    async def plate_1_journey(ctx: ThreadContext):
        yield target_capture
        yield incubate_2hrs
        yield post_capture_spin
        yield post_capture_wash
        yield add_detection_antibody
        yield incubate_1hr
        yield post_detection_wash
        yield post_detection_shake
        yield final_aspiration
        yield add_elution_buffer_b
        yield incubate_10min
        yield magnetic_pellet
        yield neutralize_and_transfer

    @orca.thread(labware=sample_plate, start=("stacker_1", DISPENSE), end="stacker_2")
    async def sample_plate_journey(ctx: ThreadContext):
        yield delid
        yield orca.join(allows=[target_capture])

    @orca.thread(
        labware=neut_plate,
        start=("stacker_8", DISPENSE),
        end="waste_1",
        contributes_to=["final_plate"],
    )
    async def neut_plate_journey(ctx: ThreadContext):
        yield orca.join(allows=[neutralize_and_transfer])
        yield neutralize_shake
        yield centrifuge_neut
        yield transfer_to_read_plate

    @orca.thread(
        labware=final_plate,
        start=("stacker_4", DISPENSE),
        end="plate_hotel",
    )
    async def final_plate_journey(ctx: ThreadContext):
        # Receive one neutralized-eluate transfer per contributing sample plate;
        # 1 join for a single submission, N for an N-plate batch. Then read.
        while ctx.has_more_work():
            yield orca.join(allows=[transfer_to_read_plate])
        yield centrifuge
        yield read

    # Stacker invariant: one labware_type per stacker, source XOR sink (single-stage
    # magazine). 300 uL tips off stackers 5 and 7, 50 uL off stacker 6.
    @orca.thread(labware=tips_sample, start=("stacker_5", DISPENSE), end="waste_1")
    async def tips_sample_journey(ctx: ThreadContext):
        yield delid
        yield orca.join(allows=[target_capture])

    @orca.thread(labware=tips_beads, start=("stacker_7", DISPENSE), end="waste_1")
    async def tips_beads_journey(ctx: ThreadContext):
        yield delid
        yield orca.join(allows=[target_capture])

    @orca.thread(labware=tips_detection, start=("stacker_7", DISPENSE), end="waste_1")
    async def tips_detection_journey(ctx: ThreadContext):
        yield delid
        yield orca.join(allows=[add_detection_antibody])

    @orca.thread(labware=tips_read, start=("stacker_5", DISPENSE), end="waste_2")
    async def tips_read_journey(ctx: ThreadContext):
        yield delid
        yield orca.join(allows=[transfer_to_read_plate])

    @orca.thread(labware=tips_buffer_b, start=("stacker_6", DISPENSE), end="waste_2")
    async def tips_buffer_b_journey(ctx: ThreadContext):
        yield delid
        yield orca.join(allows=[add_elution_buffer_b])

    @orca.thread(labware=tips_buffer_d, start=("stacker_6", DISPENSE), end="waste_2")
    async def tips_buffer_d_journey(ctx: ThreadContext):
        yield delid
        yield orca.join(allows=[neutralize_and_transfer])

    @orca.thread(labware=tips_eluate, start=("stacker_6", DISPENSE), end="waste_2")
    async def tips_eluate_journey(ctx: ThreadContext):
        yield delid
        yield orca.join(allows=[neutralize_and_transfer])

    # Deck-resident reagent threads: each trough stays on its host ML STAR's
    # carrier-25 reagent carrier. REUSE_EXISTING rebinds the persistent labware each
    # run; LEAVE_IN_PLACE keeps it on the deck. The has_more_work loop re-offers the
    # resident once per consuming sample plate (an N-group batch reuses it N times).
    @orca.thread(
        labware=bead_reservoir,
        start=("mlstar_1/carrier-25-0", REUSE_EXISTING),
        end=("mlstar_1/carrier-25-0", LEAVE_IN_PLACE),
    )
    async def bead_reservoir_thread(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[target_capture])

    @orca.thread(
        labware=detection_reservoir,
        start=("mlstar_1/carrier-25-1", REUSE_EXISTING),
        end=("mlstar_1/carrier-25-1", LEAVE_IN_PLACE),
    )
    async def detection_reservoir_thread(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[add_detection_antibody])

    @orca.thread(
        labware=buffer_b_reservoir,
        start=("mlstar_2/carrier-25-0", REUSE_EXISTING),
        end=("mlstar_2/carrier-25-0", LEAVE_IN_PLACE),
    )
    async def buffer_b_reservoir_thread(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[add_elution_buffer_b])

    @orca.thread(
        labware=buffer_d_reservoir,
        start=("mlstar_2/carrier-25-1", REUSE_EXISTING),
        end=("mlstar_2/carrier-25-1", LEAVE_IN_PLACE),
    )
    async def buffer_d_reservoir_thread(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[neutralize_and_transfer])

    # --- Workflow ---

    @orca.workflow(name="hamilton_smc_assay")
    def workflow(wf: WorkflowContext):
        wf.start(plate_1_journey)
        wf.thread(sample_plate_journey)
        wf.thread(neut_plate_journey)
        wf.thread(
            final_plate_journey,
            capacity=CapacityPolicy(max_contributions=4, overflow_action=OverflowAction.NEW),
        )
        wf.thread(tips_sample_journey)
        wf.thread(tips_beads_journey)
        wf.thread(tips_detection_journey)
        wf.thread(tips_read_journey)
        wf.thread(tips_buffer_b_journey)
        wf.thread(tips_buffer_d_journey)
        wf.thread(tips_eluate_journey)
        wf.thread(bead_reservoir_thread)
        wf.thread(detection_reservoir_thread)
        wf.thread(buffer_b_reservoir_thread)
        wf.thread(buffer_d_reservoir_thread)

    return workflow
