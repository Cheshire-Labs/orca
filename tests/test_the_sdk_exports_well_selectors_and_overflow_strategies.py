"""Well selectors and overflow strategies are public SDK names."""

import orca.sdk.wells as wells
import orca.sdk.workflow as workflow
from orca.resource_models.well_selector import WellSelector
from orca.workflow_models.overflow_strategy import RejectStrategy, SequentialStashStrategy


def test_the_well_selectors_are_exported_from_orca_sdk_wells() -> None:
    for name in ("WellSelector", "well_list", "well_range", "quadrant", "all_wells"):
        assert name in wells.__all__, name
    assert wells.WellSelector is WellSelector
    assert wells.well_list(["A1", "B1"]) == WellSelector(mode="list", spec=["A1", "B1"])


def test_the_overflow_strategies_are_exported_from_orca_sdk_workflow() -> None:
    for name in ("IOverflowStrategy", "SequentialStashStrategy", "RejectStrategy"):
        assert name in workflow.__all__, name
    assert workflow.SequentialStashStrategy is SequentialStashStrategy
    assert workflow.RejectStrategy is RejectStrategy

