"""A deck_positions keyed by a labware name is refused where the workflow is written, not mid-run."""

import json

import pytest

import orca.orca as orca
from orca.devices.shaker import Shaker
from orca.sdk.labware import PlateTemplate
from orca.workflow_models.action_context import ActionContext

PLATE = "Cor_Falcon_96_wellplate_340ul_Fb_Black"


def test_a_deck_position_keyed_by_the_plate_name_is_refused_with_the_right_shape() -> None:
    sample = PlateTemplate("sample", labware_type=PLATE)

    async def quant(ctx: ActionContext) -> None:
        del ctx

    with pytest.raises(TypeError, match="key it by the labware template object itself"):
        orca.action(device=Shaker("shaker_1"), inputs=[sample], deck_positions=json.loads('{"sample": "C2-slot"}'))(quant)


def test_a_deck_position_for_labware_the_action_does_not_handle_is_refused() -> None:
    sample = PlateTemplate("sample", labware_type=PLATE)
    stranger = PlateTemplate("stranger", labware_type=PLATE)

    async def quant(ctx: ActionContext) -> None:
        del ctx

    with pytest.raises(TypeError, match="not among its inputs or outputs"):
        orca.action(device=Shaker("shaker_1"), inputs=[sample], deck_positions={stranger: "C2-slot"})(quant)


def test_a_deck_position_keyed_by_the_template_is_accepted() -> None:
    sample = PlateTemplate("sample", labware_type=PLATE)

    async def quant(ctx: ActionContext) -> None:
        del ctx

    step = orca.action(device=Shaker("shaker_1"), inputs=[sample], deck_positions={sample: "C2-slot"})(quant)

    assert step.deck_positions == {sample: "C2-slot"}
