"""Workflow definition for the Bravo SMC assay example.

This is the SAME IL-6 immunoassay as ``examples/hamilton_smc``, with
the SAME method flow and device steps, differing in ONE respect: the liquid-handler
steps are driven by protocol-file strings (``run_protocol("...pro")``) on a deckless
``LiquidHandlerProtocol`` rather than inline PyLabRobot methods. There is no Bravo
driver, so the Bravo is modelled as a protocol-string device; the per-well pipetting
lives inside the opaque ``.pro`` files. Everything else -- the assay step sequence,
shaker speeds, centrifuge settings, the fresh neutralization plate, and the
four-plates-into-one-384-read-plate pooling -- mirrors the Hamilton example.

The assay plate arrives PRE-LOADED (standard curve + samples, prepared upstream);
reagents are prepared and live on the Bravo's own deck inside the ``.pro`` files.

  Target Capture     sample + capture beads into the assay plate, 2 h capture.
  Post-Capture Wash  magnetic wash.
  Detection          detection antibody, 1 h.
  Post-Detection     4-cycle wash, 90 s shake, final aspiration.
  Elution            Elution Buffer B, 10 min.
  Neutralize         Buffer D into a fresh plate; eluate transferred onto it.
  Read               neutralized eluate to the 384-well SMC read plate.

Submission shape:
  ``final_plate`` is ``GroupSharing.SHARED_ACROSS_GROUPS +
  SubmissionBatching.BATCHABLE``: N-group submissions coalesce into one read plate,
  and ``batch_mode="JOIN_EXISTING"`` streams later submissions into an in-flight
  read plate until ``submissions.close_execution`` drains it. The default
  single-group + STANDALONE shape produces one fresh read plate per request.
"""

import orca.orca as orca

from orca.spawn import DISPENSE
from orca.devices.centrifuge import Centrifuge
from orca.devices.devices import Delidder, LiquidHandlerProtocol, PlateWasher, Reader
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
from orca.state.records import DeclaredTracking
from orca.sdk.build import Topology
from orca.sdk.labware import AnyLabwareTemplate, PlateTemplate, TipRackTemplate
from orca.sdk.wells import column_stripes_96
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
tips_96 = TipRackTemplate("tips_96", labware_type="hamilton_96_tiprack_300uL_filter", with_tips=True)
tips_384 = TipRackTemplate("tips_384", labware_type="hamilton_96_tiprack_50uL_filter", with_tips=True)


def build_workflow(topology: Topology) -> WorkflowTemplate:
    """Build the Bravo SMC assay workflow bound to the given topology."""

    biotek_1 = topology.device("biotek_1", PlateWasher)
    biotek_2 = topology.device("biotek_2", PlateWasher)
    bravo_96 = topology.device("bravo_96", LiquidHandlerProtocol)
    bravo_384 = topology.device("bravo_384", LiquidHandlerProtocol)
    centrifuge_device = topology.device("centrifuge", Centrifuge)
    delidder = topology.device("delidder", Delidder)
    smc_pro = topology.device("smc_pro", Reader)
    shaker_collection = topology.pool("shaker_collection")

    # --- Actions ---

    @orca.action(device=bravo_96, inputs=[sample_plate, tips_96, plate_1])
    async def run_target_capture(ctx: ActionContext):
        # 75 uL sample then 100 uL capture beads into the assay plate (opaque .pro).
        await ctx.device().run_protocol("target_capture.pro", {})

    @orca.action(device=shaker_collection, inputs=[plate_1])
    async def shake_2hrs(ctx: ActionContext):
        # 2 h capture at Jitterbug setting #4 (~875 rpm).
        await ctx.device().shake(duration=7200, speed=875)

    @orca.action(device=centrifuge_device, inputs=[plate_1])
    async def run_post_capture_spin(ctx: ActionContext):
        await ctx.device().centrifuge(g=1100, duration=60)

    @orca.action(device=biotek_1, inputs=[plate_1])
    async def run_post_capture_wash(ctx: ActionContext):
        await ctx.device().run_protocol("post_capture_wash.pro", {})

    @orca.action(device=bravo_96, inputs=[plate_1, tips_96])
    async def run_add_detection_antibody(ctx: ActionContext):
        await ctx.device().run_protocol("add_detection_antibody.pro", {})

    @orca.action(device=shaker_collection, inputs=[plate_1])
    async def shake_1hr(ctx: ActionContext):
        # 1 h detection at Jitterbug setting #5 (~1000 rpm).
        await ctx.device().shake(duration=3600, speed=1000)

    @orca.action(device=biotek_2, inputs=[plate_1])
    async def run_post_detection_wash(ctx: ActionContext):
        # 4-cycle Pre-Transfer wash (4CYCPRE), distinct from the post-capture wash.
        await ctx.device().run_protocol("post_detection_wash.pro", {})

    @orca.action(device=shaker_collection, inputs=[plate_1])
    async def shake_90s(ctx: ActionContext):
        # 90 s post-detection shake at Jitterbug setting #3 (~750 rpm).
        await ctx.device().shake(duration=90, speed=750)

    @orca.action(device=biotek_2, inputs=[plate_1])
    async def run_final_aspiration(ctx: ActionContext):
        await ctx.device().run_protocol("final_aspiration.pro", {})

    # The 3 bravo_384 actions each claim a distinct stripe of the 96-position
    # tips_384 rack; together they deplete it across 3 invocations, so
    # TipRackInstance.can_continue() flips False and the tip receiver loop exits.
    _s1, _s2, _s3 = column_stripes_96(3)

    @orca.action(
        device=bravo_384, inputs=[plate_1, tips_384],
        declares=DeclaredTracking(tips_used={"tips_384": _s1}),
    )
    async def run_add_elution_buffer_b(ctx: ActionContext):
        await ctx.device().run_protocol("add_elution_buffer_b.pro", {})

    @orca.action(device=shaker_collection, inputs=[plate_1])
    async def shake_10min(ctx: ActionContext):
        # 10 min elution at Jitterbug setting #5 (~1000 rpm).
        await ctx.device().shake(duration=600, speed=1000)

    @orca.action(device=biotek_2, inputs=[plate_1])
    async def run_magnetic_pellet(ctx: ActionContext):
        await ctx.device().run_protocol("magnetic_pellet.pro", {})

    # Pre-dispense Buffer D into the fresh neutralization plate, then transfer the
    # eluate onto it (neutralizing). Driven by the assay plate; the neutralization
    # plate joins as a passive destination.
    @orca.action(
        device=bravo_384, inputs=[plate_1, neut_plate, tips_384],
        declares=DeclaredTracking(tips_used={"tips_384": _s2}),
    )
    async def run_neutralize_and_transfer(ctx: ActionContext):
        await ctx.device().run_protocol("neutralize_and_transfer.pro", {})

    @orca.action(device=shaker_collection, inputs=[neut_plate])
    async def shake_neutralize(ctx: ActionContext):
        # 2 min neutralization mix at Jitterbug setting #5 (~1000 rpm).
        await ctx.device().shake(duration=120, speed=1000)

    @orca.action(device=centrifuge_device, inputs=[neut_plate])
    async def spin_neut(ctx: ActionContext):
        await ctx.device().centrifuge(g=1100, duration=60)

    @orca.action(
        device=bravo_384, inputs=[neut_plate, final_plate, tips_384],
        declares=DeclaredTracking(tips_used={"tips_384": _s3}),
    )
    async def run_transfer_to_read_plate(ctx: ActionContext):
        # Neutralized eluate into the 384-well SMC read plate.
        await ctx.device().run_protocol("transfer_to_read_plate.pro", {})

    @orca.action(device=centrifuge_device, inputs=[final_plate])
    async def spin_plate(ctx: ActionContext):
        await ctx.device().centrifuge(g=1100, duration=60)

    @orca.action(device=smc_pro, inputs=[final_plate])
    async def read_plate(ctx: ActionContext):
        await ctx.device().read("read.pro", "results.csv")

    @orca.action(device=delidder, inputs=[AnyLabwareTemplate()])
    async def delid_plate(ctx: ActionContext):
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
        contributes_to=["neut_plate", "tips_384"],
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
        contributes_to=["final_plate", "tips_384"],
    )
    async def neut_plate_journey(ctx: ThreadContext):
        yield orca.join(allows=[neutralize_and_transfer])
        yield neutralize_shake
        yield centrifuge_neut
        yield transfer_to_read_plate

    @orca.thread(
        labware=final_plate,
        start=("stacker_4", DISPENSE),
        end=[f"hotel_pad_{i}" for i in range(1, 13)],
    )
    async def final_plate_journey(ctx: ThreadContext):
        # Submission-driven loop: join as many read-plate transfers as arrive; exit
        # when the SubmissionManager closes the slot. One iteration for a single
        # submission, N for an N-plate batch. Then centrifuge and read.
        while ctx.has_more_work():
            yield orca.join(allows=[transfer_to_read_plate])
        yield centrifuge
        yield read

    @orca.thread(labware=tips_96, start=("stacker_5", DISPENSE), end="waste_1")
    async def tips_96_journey(ctx: ThreadContext):
        yield delid
        yield orca.join(allows=[target_capture, add_detection_antibody])

    @orca.thread(labware=tips_384, start=("stacker_6", DISPENSE), end="stacker_7")
    async def tips_384_journey(ctx: ThreadContext):
        yield delid
        # Exit on either rack exhaustion (rack fully used across the 3 stripes) or
        # slot closure (partial-fill case under per-group auto-spawn). has_more_work
        # reflects slot state; can_continue reflects tip depletion.
        while ctx.has_more_work():
            yield orca.join(allows=[add_elution_buffer_b, neutralize_and_transfer,
                                    transfer_to_read_plate])
            if not await ctx.labware.can_continue():
                break
            # Park beside bravo_384, one shelf per rack. Shelf 1 is left out
            # of this pool so a retiring read plate can always reach one.
            yield orca.park([f"hotel_pad_{i}" for i in range(2, 13)])

    # --- Workflow ---

    @orca.workflow(name="smc_assay")
    def workflow(wf: WorkflowContext):
        wf.start(plate_1_journey)
        wf.thread(sample_plate_journey)
        wf.thread(neut_plate_journey)
        wf.thread(tips_96_journey)
        wf.thread(tips_384_journey)
        wf.thread(final_plate_journey)

    return workflow
