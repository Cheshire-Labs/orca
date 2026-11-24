"""Each sample plate is tagged with one particular index plate, chosen by barcode.

The sample thread names its partner with ``ctx.set_partner_constraint``. From then on its
steps run only with the index plate that carries that barcode: the engine takes that plate
from wherever it stands, and pauses the sample thread when it cannot.

To run it, register the plates, then submit one group per sample plate by barcode:

- index plates ``IDX-1`` at ``index_hotel_1`` and ``IDX-2`` at ``index_hotel_2``
- sample plates ``S-1`` and ``S-2``
"""

import orca.orca as orca

from orca.sdk.build import Topology
from orca.sdk.devices import LiquidHandlerProtocol
from orca.sdk.labware import PlateTemplate
from orca.sdk.workflow import (
    ActionContext,
    MethodContext,
    ThreadContext,
    WorkflowContext,
    WorkflowTemplate,
)


sample_plate = PlateTemplate("sample_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
index_plate = PlateTemplate("index_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")

# The sample sheet: which index plate each sample plate is tagged with.
INDEX_PLATE_FOR_SAMPLE_PLATE = {"S-1": "IDX-1", "S-2": "IDX-2"}


def build_workflow(topology: Topology) -> WorkflowTemplate:
    tag_station = topology.device("tag_station", LiquidHandlerProtocol)

    @orca.action(device=tag_station, inputs=[sample_plate, index_plate])
    async def add_indexes(ctx: ActionContext) -> None:
        await ctx.device().run_protocol("add_indexes.pro", {})

    @orca.method
    async def tag_samples(ctx: MethodContext):
        yield add_indexes

    @orca.thread(labware=sample_plate, start="sample_pad", end="waste", contributes_to=["index_plate"])
    async def sample_plate_thread(ctx: ThreadContext):
        index_barcode = INDEX_PLATE_FOR_SAMPLE_PLATE.get(ctx.labware.barcode or "")
        if index_barcode is not None:
            ctx.set_partner_constraint("index_plate", {"barcode": index_barcode})
        yield tag_samples

    @orca.thread(labware=index_plate, start="index_hotel_1", end="waste")
    async def index_plate_thread(ctx: ThreadContext):
        yield orca.join(allows=[tag_samples])

    @orca.workflow(name="index_plate_pairing")
    def workflow(wf: WorkflowContext):
        wf.start(sample_plate_thread)
        wf.thread(index_plate_thread)

    return workflow
