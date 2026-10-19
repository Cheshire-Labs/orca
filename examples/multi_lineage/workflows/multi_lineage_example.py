"""Workflow definition for the multi-lineage example.

A PER_GROUP ``sample`` plate plus a SHARED_ACROSS_GROUPS ``reservoir``.
One workflow scales from N=1 to N=many groups without code changes -- the
reservoir receiver accepts N mix contributions via the
``while ctx.has_more_work(): yield orca.join(...)`` pattern, and the
engine closes the slot when every contributor has terminated.

Auto-spawn of the shared reservoir is implicit in ``wf.thread(...)``:
the receiver template is registered, and the engine instantiates it when
the first sample contribution arrives.
"""

import orca.orca as orca

from orca.devices.devices import LiquidHandlerProtocol
from orca.devices.shaker import Shaker
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate


# --- Labware templates (stateless, safe to share across builds) ---

sample = PlateTemplate("sample", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
reservoir = PlateTemplate(
    "reservoir",
    labware_type="AGenBio_1_troughplate_190000uL_Fl",
    group_sharing=GroupSharing.SHARED_ACROSS_GROUPS,
    submission_batching=SubmissionBatching.BATCHABLE,
)


def build_workflow(topology: Topology) -> WorkflowTemplate:
    """Build the multi-lineage workflow bound to the given topology."""

    # Mixing sample into the reservoir is pipetting, so the station is a
    # protocol-driven liquid handler with one deck position per input.
    station = topology.device("station", LiquidHandlerProtocol)
    reservoir_station = topology.device("reservoir_station", Shaker)

    @orca.action(device=station, inputs=[sample, reservoir])
    async def mix(ctx: ActionContext) -> None:
        await ctx.device().run_protocol("mix_sample_into_reservoir.pro", {})

    @orca.action(device=reservoir_station, inputs=[reservoir])
    async def rinse(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=200)

    @orca.method
    async def sample_method(ctx: MethodContext):
        yield mix

    @orca.method
    async def reservoir_method(ctx: MethodContext):
        yield rinse

    @orca.thread(
        labware=sample,
        start="start_pad",
        end="waste",
        contributes_to=["reservoir"],
    )
    async def sample_thread(ctx: ThreadContext):
        yield sample_method

    @orca.thread(labware=reservoir, start="reservoir_pad", end="reservoir_pad")
    async def reservoir_thread(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[sample_method])
            if ctx.has_more_work():
                yield orca.park("reservoir_pad")
        yield reservoir_method

    @orca.workflow(name="multi_lineage_example")
    def workflow(wf: WorkflowContext):
        wf.start(sample_thread)
        # wf.thread() registers reservoir_thread for auto-spawn; the engine
        # instantiates it when the first sample contribution arrives.
        wf.thread(reservoir_thread)

    return workflow
