"""Pure-function tests for ledger_projections.

Exercises well_volume / well_volumes / tips_present / tips_used / op_count
/ sample_events_at against hand-built lists of OperationRecord. No runtime
or OpsHistory required -- that integration is covered in
test_ops_history.py.
"""
import time

import pytest

from orca.state.projections import (
    op_count,
    sample_events_at,
    tips_present,
    tips_used,
    well_volume,
    well_volumes,
)
from orca.state.records import (
    Aspirate96Details,
    AspirateDetails,
    DeviceOperation,
    Dispense96Details,
    DispenseDetails,
    GenericOperationDetails,
    InitialStateDetails,
    OperationRecord,
    SetVolumeDetails,
    TipDropDetails,
    TipPickUpDetails,
)


def _op(operation: DeviceOperation, affected: list[str], details, group_id: str | None = None) -> OperationRecord:
    return OperationRecord(
        operation=operation,
        device_name="lh",
        affected_labware=affected,
        action_id="a",
        thread_id="t",
        details=details,
        timestamp=time.time(),
        group_id=group_id,
    )


def _seed_wells(labware: str, wells: dict[str, float], single_pool: bool = False) -> OperationRecord:
    return _op(DeviceOperation.INITIAL_STATE, [labware], InitialStateDetails(labware=labware, well_volumes=wells, single_pool=single_pool))


def _seed_tips(rack: str, positions: list[str]) -> OperationRecord:
    return _op(DeviceOperation.INITIAL_STATE, [rack], InitialStateDetails(labware=rack, tip_positions_present=positions))


def _aspirate(labware: str, positions: list[str], volumes: list[float]) -> OperationRecord:
    return _op(DeviceOperation.ASPIRATE, [labware], AspirateDetails(labware=labware, positions=positions, volumes=volumes))


def _dispense(labware: str, positions: list[str], volumes: list[float]) -> OperationRecord:
    return _op(DeviceOperation.DISPENSE, [labware], DispenseDetails(labware=labware, positions=positions, volumes=volumes))


def _pickup(rack: str, positions: list[str]) -> OperationRecord:
    return _op(DeviceOperation.PICK_UP_TIPS, [rack], TipPickUpDetails(tip_rack=rack, positions=positions))


def _drop_to_rack(rack: str, positions: list[str]) -> OperationRecord:
    return _op(DeviceOperation.DROP_TIPS, [rack], TipDropDetails(tip_rack=rack, positions=positions, to_waste=False))


def _drop_to_waste(rack: str, positions: list[str]) -> OperationRecord:
    return _op(DeviceOperation.DROP_TIPS, [rack], TipDropDetails(tip_rack=rack, positions=positions, to_waste=True))


def _aspirate96(labware: str, volume: float) -> OperationRecord:
    return _op(DeviceOperation.ASPIRATE96, [labware], Aspirate96Details(labware=labware, volume=volume))


def _dispense96(labware: str, volume: float) -> OperationRecord:
    return _op(DeviceOperation.DISPENSE96, [labware], Dispense96Details(labware=labware, volume=volume))


def _mix(labware: str) -> OperationRecord:
    return _op(DeviceOperation.MIX, [labware], GenericOperationDetails(command="mix", args_repr="(...)"))


def _set_volume(labware: str, wells: dict[str, float]) -> OperationRecord:
    return _op(DeviceOperation.SET_VOLUME, [labware], SetVolumeDetails(labware=labware, well_volumes=wells))


class TestSetVolumeFold:
    """Operator SET_VOLUME is an absolute overwrite of its named wells at its
    position in history; later aspirate/dispense deltas still apply on top."""

    def test_set_volume_overwrites_seeded_well_absolutely(self) -> None:
        ops = [_seed_wells("p", {"A1": 100.0}), _set_volume("p", {"A1": 30.0})]
        assert well_volume(ops, "p", "A1") == 30.0

    def test_set_volume_then_aspirate_dispense_apply_as_deltas(self) -> None:
        ops = [
            _seed_wells("p", {"A1": 100.0}),
            _set_volume("p", {"A1": 50.0}),
            _aspirate("p", ["A1"], [20.0]),
            _dispense("p", ["A1"], [5.0]),
        ]
        assert well_volume(ops, "p", "A1") == 35.0

    def test_set_volume_seeds_a_never_seeded_well(self) -> None:
        ops = [_set_volume("p", {"B2": 75.0})]
        assert well_volumes(ops, "p") == {"B2": 75.0}

    def test_set_volume_only_touches_named_wells(self) -> None:
        ops = [
            _seed_wells("p", {"A1": 100.0, "A2": 100.0}),
            _set_volume("p", {"A1": 10.0}),
        ]
        assert well_volumes(ops, "p") == {"A1": 10.0, "A2": 100.0}

    def test_later_set_volume_wins_over_earlier_set_volume(self) -> None:
        ops = [
            _set_volume("p", {"A1": 10.0}),
            _set_volume("p", {"A1": 80.0}),
        ]
        assert well_volume(ops, "p", "A1") == 80.0

    def test_set_volume_filters_by_labware_name(self) -> None:
        ops = [
            _seed_wells("p1", {"A1": 100.0}),
            _seed_wells("p2", {"A1": 100.0}),
            _set_volume("p1", {"A1": 5.0}),
        ]
        assert well_volume(ops, "p1", "A1") == 5.0
        assert well_volume(ops, "p2", "A1") == 100.0


class TestNinetySixHeadVolumeFold:
    """A 96-head op folds one volume per well on a plate grid, but the whole head
    on a single pool (trough): the pool loses head_size x volume, not volume once."""

    def test_aspirate96_plate_drops_every_known_well(self) -> None:
        ops = [_seed_wells("p", {"A1": 100.0, "A2": 100.0, "B1": 100.0}), _aspirate96("p", 10.0)]
        assert well_volumes(ops, "p") == {"A1": 90.0, "A2": 90.0, "B1": 90.0}

    def test_dispense96_plate_raises_every_known_well(self) -> None:
        ops = [_seed_wells("p", {"A1": 0.0, "A2": 0.0}), _dispense96("p", 10.0)]
        assert well_volumes(ops, "p") == {"A1": 10.0, "A2": 10.0}

    def test_aspirate96_single_pool_drains_by_full_head(self) -> None:
        ops = [_seed_wells("trough", {"A1": 100_000.0}, single_pool=True), _aspirate96("trough", 10.0)]
        assert well_volume(ops, "trough", "A1") == 99_040.0

    def test_dispense96_single_pool_credits_by_full_head(self) -> None:
        ops = [_seed_wells("trough", {"A1": 0.0}, single_pool=True), _dispense96("trough", 10.0)]
        assert well_volume(ops, "trough", "A1") == 960.0

    def test_aspirate96_one_well_plate_is_not_full_head(self) -> None:
        # Single-pool-ness comes from the seed flag, not the known-well count:
        # a plate with one seeded well still folds 1x, never the full-head 96x.
        ops = [_seed_wells("p", {"A1": 100.0}), _aspirate96("p", 10.0)]
        assert well_volumes(ops, "p") == {"A1": 90.0}

    def test_dispense96_one_well_plate_is_not_full_head(self) -> None:
        ops = [_seed_wells("p", {"A1": 0.0}), _dispense96("p", 10.0)]
        assert well_volumes(ops, "p") == {"A1": 10.0}


class TestMixDoesNotMoveVolume:
    """Mix is aspirate+dispense in place (net-zero), so the ledger must not move
    volume -- it is recorded as a generic op the volume fold ignores."""

    def test_mix_leaves_plate_well_unchanged(self) -> None:
        ops = [_seed_wells("p", {"A1": 100.0}), _mix("p")]
        assert well_volume(ops, "p", "A1") == 100.0

    def test_mix_leaves_pool_unchanged(self) -> None:
        ops = [_seed_wells("trough", {"A1": 5000.0}), _mix("trough")]
        assert well_volume(ops, "trough", "A1") == 5000.0


class TestWellVolume:
    def test_seed_then_aspirate(self) -> None:
        ops = [_seed_wells("p", {"A1": 100.0}), _aspirate("p", ["A1"], [40.0])]
        assert well_volume(ops, "p", "A1") == 60.0

    def test_seed_then_dispense(self) -> None:
        ops = [_seed_wells("p", {"A1": 0.0}), _dispense("p", ["A1"], [25.0])]
        assert well_volume(ops, "p", "A1") == 25.0

    def test_unseeded_untouched_is_none(self) -> None:
        ops = [_seed_wells("p", {"A1": 100.0})]
        assert well_volume(ops, "p", "H12") is None

    def test_overdraw_surfaces_negative_no_clamp(self) -> None:
        ops = [_seed_wells("p", {"A1": 50.0}), _aspirate("p", ["A1"], [80.0])]
        assert well_volume(ops, "p", "A1") == -30.0

    def test_unseeded_aspirate_starts_from_zero(self) -> None:
        ops = [_aspirate("p", ["A1"], [10.0])]
        assert well_volume(ops, "p", "A1") == -10.0

    def test_multi_channel_pair_by_index(self) -> None:
        ops = [
            _seed_wells("p", {"A1": 100.0, "B1": 100.0, "C1": 100.0}),
            _aspirate("p", ["A1", "B1", "C1"], [10.0, 20.0, 30.0]),
        ]
        volumes = well_volumes(ops, "p")
        assert volumes["A1"] == 90.0
        assert volumes["B1"] == 80.0
        assert volumes["C1"] == 70.0

    def test_one_to_many_fanout(self) -> None:
        ops = [
            _seed_wells("src", {"A1": 100.0}),
            _seed_wells("dst", {"B1": 0.0, "B2": 0.0, "B3": 0.0}),
            _aspirate("src", ["A1"], [90.0]),
            _dispense("dst", ["B1", "B2", "B3"], [30.0, 30.0, 30.0]),
        ]
        assert well_volume(ops, "src", "A1") == 10.0
        assert well_volume(ops, "dst", "B1") == 30.0
        assert well_volume(ops, "dst", "B3") == 30.0

    def test_aspirate_to_trash_decrements_source_only(self) -> None:
        ops = [_seed_wells("p", {"A1": 100.0}), _aspirate("p", ["A1"], [50.0])]
        # No dispense; source loses, no target gains.
        assert well_volume(ops, "p", "A1") == 50.0

    def test_filters_by_labware_name(self) -> None:
        ops = [
            _seed_wells("p1", {"A1": 100.0}),
            _seed_wells("p2", {"A1": 200.0}),
            _aspirate("p1", ["A1"], [50.0]),
        ]
        assert well_volume(ops, "p1", "A1") == 50.0
        assert well_volume(ops, "p2", "A1") == 200.0


class TestTipsPresent:
    def test_seeded_and_picked(self) -> None:
        ops = [_seed_tips("rack", ["A1", "A2", "A3"]), _pickup("rack", ["A1"])]
        assert tips_present(ops, "rack") == {"A2", "A3"}

    def test_returned_to_rack_is_present(self) -> None:
        ops = [_seed_tips("rack", ["A1"]), _pickup("rack", ["A1"]), _drop_to_rack("rack", ["A1"])]
        assert tips_present(ops, "rack") == {"A1"}

    def test_dropped_to_waste_stays_absent(self) -> None:
        ops = [_seed_tips("rack", ["A1", "A2"]), _pickup("rack", ["A1"]), _drop_to_waste("rack", ["A1"])]
        assert tips_present(ops, "rack") == {"A2"}


class TestTipsUsed:
    def test_picked_and_not_returned(self) -> None:
        ops = [_seed_tips("rack", ["A1", "A2", "A3"]), _pickup("rack", ["A1", "A2"])]
        assert tips_used(ops, "rack") == {"A1", "A2"}

    def test_picked_and_returned_is_not_used(self) -> None:
        ops = [_seed_tips("rack", ["A1"]), _pickup("rack", ["A1"]), _drop_to_rack("rack", ["A1"])]
        assert tips_used(ops, "rack") == set()


class TestOpCount:
    def test_default_counts_aspirates_and_dispenses(self) -> None:
        ops = [
            _aspirate("p", ["A1"], [10.0]),
            _dispense("p", ["B1"], [10.0]),
            _dispense("p", ["C1"], [20.0]),
        ]
        assert op_count(ops, "p") == 3

    def test_narrow_to_dispenses_only(self) -> None:
        ops = [
            _aspirate("p", ["A1"], [10.0]),
            _dispense("p", ["B1"], [10.0]),
            _dispense("p", ["C1"], [20.0]),
        ]
        assert op_count(ops, "p", (DeviceOperation.DISPENSE,)) == 2

    def test_narrow_to_aspirates_only(self) -> None:
        ops = [
            _aspirate("p", ["A1"], [10.0]),
            _aspirate("p", ["A2"], [10.0]),
            _dispense("q", ["B1"], [20.0]),
        ]
        assert op_count(ops, "p", (DeviceOperation.ASPIRATE,)) == 2

    def test_smc_four_quadrant_dispenses_count_as_4(self) -> None:
        # SMC final_plate receives four quadrant dispenses.
        ops = [_dispense("final", ["A1"], [10.0]) for _ in range(4)]
        assert op_count(ops, "final", (DeviceOperation.DISPENSE,)) == 4


class TestSampleEventsAt:
    def test_walks_aspirate_dispense_chain(self) -> None:
        ops = [
            _aspirate("src", ["A1"], [50.0]),
            _dispense("dst", ["B3"], [50.0]),
        ]
        src_events = sample_events_at(ops, "src", "A1")
        dst_events = sample_events_at(ops, "dst", "B3")
        assert len(src_events) == 1
        assert len(dst_events) == 1
        assert src_events[0].operation == DeviceOperation.ASPIRATE
        assert dst_events[0].operation == DeviceOperation.DISPENSE

    def test_filters_by_well_id(self) -> None:
        ops = [_dispense("p", ["A1", "B1", "C1"], [10.0, 10.0, 10.0])]
        assert len(sample_events_at(ops, "p", "A1")) == 1
        assert len(sample_events_at(ops, "p", "B1")) == 1
        assert len(sample_events_at(ops, "p", "D4")) == 0
