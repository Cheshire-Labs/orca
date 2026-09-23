"""Build-time validation: ``PlateTemplate(labware_type=<trough>)`` and similar
mismatches between the declared template class and the catalog's category
field must raise ``TemplateCategoryMismatch`` at ``bind_catalog`` time, not
``AttributeError: 'PLRTroughAdapter' object has no attribute 'barcode'``
deep in workflow execution.

The runtime has both pieces of information at bind time -- the template
class declares what it is, and the catalog row carries ``category`` --
so the mismatch is detectable up front. This test pins the contract.
"""

from unittest.mock import MagicMock

import pytest

from orca.resource_models.labware import (
    PlateTemplate,
    TemplateCategoryMismatch,
    TipRackTemplate,
    TroughTemplate,
    TubeRackTemplate,
)
from orca.runtime.labware_catalog_protocol import ILabwareCatalog, LabwareNotFound


def _catalog_returning(category: str) -> MagicMock:
    """Build an ``ILabwareCatalog`` mock whose ``get(labware_type)`` returns
    a definition with the given ``.category`` field.
    """
    catalog = MagicMock(spec=ILabwareCatalog)
    definition = MagicMock()
    definition.category = category
    catalog.get.return_value = definition
    return catalog


async def test_plate_template_with_trough_labware_raises_at_bind() -> None:
    """The Claude Desktop Round-3 failure: ``PlateTemplate('dmso_reservoir',
    labware_type='hamilton_1_trough_60ml_Vb')`` formerly failed at execution
    time with ``'PLRTroughAdapter' object has no attribute 'barcode'``.
    The fix surfaces the mismatch at build time with a clear message.
    """
    catalog = _catalog_returning("trough")
    tmpl = PlateTemplate("dmso_reservoir", labware_type="hamilton_1_trough_60ml_Vb")

    with pytest.raises(TemplateCategoryMismatch) as excinfo:
        await tmpl.bind_catalog(catalog)

    message = str(excinfo.value)
    assert "dmso_reservoir" in message
    assert "hamilton_1_trough_60ml_Vb" in message
    assert "trough" in message.lower()
    assert "TroughTemplate" in message


async def test_trough_template_with_plate_labware_raises_at_bind() -> None:
    catalog = _catalog_returning("plate")
    tmpl = TroughTemplate("misnamed", labware_type="Cor_96_wellplate_360ul_Fb")

    with pytest.raises(TemplateCategoryMismatch) as excinfo:
        await tmpl.bind_catalog(catalog)

    message = str(excinfo.value)
    assert "misnamed" in message
    assert "plate" in message.lower()
    assert "PlateTemplate" in message


async def test_tip_rack_template_with_plate_labware_raises_at_bind() -> None:
    catalog = _catalog_returning("plate")
    tmpl = TipRackTemplate("misnamed_tips", labware_type="Cor_96_wellplate_360ul_Fb", with_tips=True)

    with pytest.raises(TemplateCategoryMismatch) as excinfo:
        await tmpl.bind_catalog(catalog)

    message = str(excinfo.value)
    assert "misnamed_tips" in message
    assert "plate" in message.lower()
    assert "PlateTemplate" in message


async def test_tube_rack_template_with_plate_labware_raises_at_bind() -> None:
    catalog = _catalog_returning("plate")
    tmpl = TubeRackTemplate("misnamed_tubes", labware_type="Cor_96_wellplate_360ul_Fb")

    with pytest.raises(TemplateCategoryMismatch) as excinfo:
        await tmpl.bind_catalog(catalog)

    message = str(excinfo.value)
    assert "PlateTemplate" in message


async def test_plate_template_with_plate_labware_does_not_raise() -> None:
    """Control: matching category passes the check."""
    catalog = _catalog_returning("plate")
    tmpl = PlateTemplate("dest_plate", labware_type="Cor_96_wellplate_360ul_Fb")
    await tmpl.bind_catalog(catalog)


async def test_trough_template_with_trough_labware_does_not_raise() -> None:
    catalog = _catalog_returning("trough")
    tmpl = TroughTemplate("dmso_reservoir", labware_type="hamilton_1_trough_60ml_Vb")
    await tmpl.bind_catalog(catalog)


async def test_tip_rack_template_with_tip_rack_labware_does_not_raise() -> None:
    catalog = _catalog_returning("tip_rack")
    tmpl = TipRackTemplate("tips_96", labware_type="HTF_96", with_tips=True)
    await tmpl.bind_catalog(catalog)


async def test_unknown_labware_type_does_not_raise_from_category_check() -> None:
    """When the labware_type is not in the catalog, the category check
    silently passes. ``LabwareNotFound`` is raised by ``create_instance``
    on first use; this check is a category guard, not a presence guard.
    """
    catalog = MagicMock(spec=ILabwareCatalog)
    catalog.get.side_effect = LabwareNotFound("not_in_catalog")
    tmpl = PlateTemplate("ghost", labware_type="not_in_catalog")
    await tmpl.bind_catalog(catalog)
