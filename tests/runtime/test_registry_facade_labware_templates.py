"""A labware template row names the template class and the catalog key."""

from unittest.mock import MagicMock

from orca.resource_models.labware import PlateTemplate
from orca.runtime.facades.registry import RegistryFacade
from orca.runtime.registries.null_gateway_registry import NullDeviceConnectionSource


def _facade_with(templates: list[PlateTemplate]) -> RegistryFacade:
    system = MagicMock()
    system.labware_templates = templates
    return RegistryFacade(
        system=system,
        list_reservations_fn=lambda eid: [],
        cancel_reservation_fn=lambda eid, rid: None,
        connections=NullDeviceConnectionSource(),
    )


def test_a_labware_template_row_carries_the_catalog_key() -> None:
    facade = _facade_with([
        PlateTemplate("sample_plate", labware_type="Cor_96_wellplate_360ul_Fb"),
    ])

    [row] = facade.list_labware_templates()

    assert row.labware_type == "Cor_96_wellplate_360ul_Fb"
    assert row.type_name == "PlateTemplate"
