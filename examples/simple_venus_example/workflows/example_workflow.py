"""Workflow definition for the Venus example.

Runs Hamilton Venus .hsl protocols and transfers plates by human.

Pattern:
  * Labware templates are stateless -- declared at module scope.
  * ``build_workflow(topology)`` closes over the Venus device for the
    ``@orca.action`` decorators.
"""

import orca.orca as orca

from orca.devices.venus import Venus
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate


# --- Labware templates (stateless, safe to share across builds) ---

sample_plate = PlateTemplate("sample_plate", labware_type="Thermo_Nunc_96_well_plate_1300uL_Rb")
transfer_plate = PlateTemplate("transfer_plate", labware_type="Thermo_Nunc_96_well_plate_1300uL_Rb")


def build_workflow(topology: Topology) -> WorkflowTemplate:
    """Build the Venus example workflow bound to the given topology."""

    ml_star = topology.device("ml_star_position_1", Venus)

    @orca.action(device=ml_star, inputs=[sample_plate], deck_positions={sample_plate: "sample_site"})
    async def run_variable_test(ctx: ActionContext):
        await ctx.device().run_protocol(
            "Cheshire Labs\\VariableAccessTesting.hsl",
            {"strParam": "strParam value transmitted", "intParam": 123, "fltParam": 1.003},
        )

    @orca.action(
        device=ml_star,
        inputs=[sample_plate, transfer_plate],
        deck_positions={sample_plate: "sample_site", transfer_plate: "transfer_site"},
    )
    async def run_plate_stamp(ctx: ActionContext):
        await ctx.device().run_protocol(
            "Cheshire Labs\\SimplePlateStamp.hsl",
            {"numOfPlates": 1, "waterVol": 30, "dyeVol": 10, "wait": 1, "tipEjectPos": 2, "clld": 1},
        )

    @orca.method
    async def example_method_1(ctx: MethodContext):
        yield run_variable_test

    @orca.method
    async def transfer_method(ctx: MethodContext):
        yield run_plate_stamp

    @orca.thread(labware=sample_plate, start="plate_pad_1", end="plate_pad_2")
    async def sample_plate_thread(ctx: ThreadContext):
        yield example_method_1
        yield transfer_method

    @orca.thread(labware=transfer_plate, start="plate_pad_3", end="plate_pad_4")
    async def transfer_plate_thread(ctx: ThreadContext):
        yield orca.join()

    @orca.workflow(name="example_workflow")
    def example_workflow(wf: WorkflowContext):
        wf.start(sample_plate_thread)
        wf.thread(transfer_plate_thread)

    return example_workflow
