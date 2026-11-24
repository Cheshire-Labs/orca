"""`PlateTemplate(with_lid=True)` is refused as not implemented: nothing tracks a lid yet."""

import pytest

from orca.resource_models.labware import PlateTemplate
from tests.sdk_features.kit import PLATE_TYPE


def test_a_lidded_plate_is_refused_when_it_is_declared() -> None:
    with pytest.raises(NotImplementedError, match="lids are not supported"):
        PlateTemplate("plate", labware_type=PLATE_TYPE, with_lid=True)
