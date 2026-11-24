"""A plate declared with a lid is refused when the workflow is built: nothing tracks the lid yet."""
import pytest

from orca.sdk.labware import PlateTemplate


def test_a_plate_with_a_lid_is_refused_and_says_lids_are_not_supported() -> None:
    with pytest.raises(NotImplementedError, match="lid"):
        PlateTemplate("sample", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black", with_lid=True)


def test_a_plate_without_a_lid_builds() -> None:
    assert PlateTemplate("sample", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black").name == "sample"
