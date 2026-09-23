"""`PlateInstance.has_lid` ergonomic passthrough.

Mirrors PLR's `Plate.has_lid()` via the IPlate adapter chain. Workflows
branching on lid presence get `instance.has_lid` instead of having to
reach through `instance.plate.has_lid`.
"""

from unittest.mock import MagicMock

import pytest

from orca.resource_models.labware import PlateInstance


def _make_fake_plate(*, has_lid: bool, name: str = "p") -> MagicMock:
    plate = MagicMock()
    plate.name = name
    plate.model = "fake-model"
    plate.barcode = None
    plate.has_lid = has_lid
    return plate


@pytest.mark.parametrize("has_lid", [True, False])
def test_has_lid_passthrough(has_lid: bool) -> None:
    inst = PlateInstance(_make_fake_plate(has_lid=has_lid), template_name="p", labware_type="p")
    assert inst.has_lid is has_lid


def test_has_lid_tracks_live_plate_state() -> None:
    """has_lid re-reads the plate each access, so removing the lid after
    construction is reflected (it is not snapshotted at __init__)."""
    plate = _make_fake_plate(has_lid=True)
    inst = PlateInstance(plate, template_name="p", labware_type="p")
    assert inst.has_lid is True
    plate.has_lid = False
    assert inst.has_lid is False
    plate.has_lid = True
    assert inst.has_lid is True
