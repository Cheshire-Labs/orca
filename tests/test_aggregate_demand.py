"""ExecutingMethod.aggregate_demand unions declares across all actions.

Filter keys on labware_name: only entries referencing that labware
contribute. Tip-rack demand on rack A doesn't pollute aggregate for
plate B.
"""
from unittest.mock import MagicMock

import pytest

from orca.state.records import (
    DeclaredTracking,
    DeclaredVolumeTransfer,
)
from orca.workflow_models.method import ExecutingMethod


def _stub_action(declares: DeclaredTracking | None) -> MagicMock:
    action = MagicMock()
    action.declares = declares
    return action


def _stub_executing_method(actions: list) -> ExecutingMethod:
    """Build an ExecutingMethod-like object via direct construction bypass.

    We only need .actions + .aggregate_demand; skip the async / event-bus /
    tracking-context machinery.
    """
    em = ExecutingMethod.__new__(ExecutingMethod)
    em._method = MagicMock()
    em._method.actions = actions
    return em


class TestAggregateDemandFilter:
    def test_none_when_no_action_declares(self) -> None:
        em = _stub_executing_method([_stub_action(None), _stub_action(None)])
        assert em.aggregate_demand("plate1") is None

    def test_unions_tips_used_only_for_matching_rack(self) -> None:
        em = _stub_executing_method([
            _stub_action(DeclaredTracking(tips_used={"rack_a": ["A1", "A2"]})),
            _stub_action(DeclaredTracking(tips_used={"rack_a": ["A3"], "rack_b": ["A1"]})),
        ])
        rack_a_demand = em.aggregate_demand("rack_a")
        assert rack_a_demand is not None
        assert rack_a_demand.tips_used == {"rack_a": ["A1", "A2", "A3"]}

    def test_unaffected_labware_does_not_pollute(self) -> None:
        em = _stub_executing_method([
            _stub_action(DeclaredTracking(tips_used={"rack_a": ["A1"]})),
        ])
        # Asking about rack_b: no tips, so tips_used is None on the result.
        rack_b_demand = em.aggregate_demand("rack_b")
        assert rack_b_demand is not None  # seen declares, just nothing relevant
        assert rack_b_demand.tips_used is None

    def test_unions_volume_transfers_by_source_or_target(self) -> None:
        em = _stub_executing_method([
            _stub_action(DeclaredTracking(volume_transferred=[
                DeclaredVolumeTransfer(source="source", target="dest", volume_ul=50.0),
                DeclaredVolumeTransfer(source="other", target="irrelevant", volume_ul=10.0),
            ])),
            _stub_action(DeclaredTracking(volume_transferred=[
                DeclaredVolumeTransfer(source="dest", target="trash", volume_ul=25.0),
            ])),
        ])
        dest_demand = em.aggregate_demand("dest")
        assert dest_demand is not None
        assert dest_demand.volume_transferred is not None
        assert len(dest_demand.volume_transferred) == 2  # one where dest is target, one where dest is source

    def test_unions_wells_used(self) -> None:
        em = _stub_executing_method([
            _stub_action(DeclaredTracking(wells_used={"plate1": ["A1"]})),
            _stub_action(DeclaredTracking(wells_used={"plate1": ["A2", "A3"]})),
        ])
        demand = em.aggregate_demand("plate1")
        assert demand is not None
        assert demand.wells_used == {"plate1": ["A1", "A2", "A3"]}
