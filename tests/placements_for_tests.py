"""The placement ledger a unit test's positions share when no system build binds them."""

from orca.resource_models.labware_directory import LabwareDirectory, LabwarePlacements
from orca.state.placement import PlacementLedger

_current = LabwarePlacements(PlacementLedger(), LabwareDirectory())


def fresh_test_placements() -> LabwarePlacements:
    """Start an empty ledger for the next test. The autouse fixture in conftest calls it."""
    global _current
    _current = LabwarePlacements(PlacementLedger(), LabwareDirectory())
    return _current


def this_tests_placements() -> LabwarePlacements:
    """The ledger every position this test made without a build is bound to."""
    return _current
