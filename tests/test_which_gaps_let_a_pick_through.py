"""Which observation gaps let a pick through at a position the record calls empty."""

import pytest

from tests.memory_activity import memory_ops_store
from orca.state.ops_history import OpsHistory
from orca.state.projections import (
    contents_provenance,
    unsettled_gaps,
    went_unobserved,
)
from orca.state.provenance import Provenance
from orca.state.records import (
    DeviceOperation,
    InitialStateDetails,
    ObservationGapCause,
    OperationRecord,
    TipPickUpDetails,
    TrackingRecord,
    TrackingSource,
)

_LH = "lh_1"
_RACK = "tips_96"
_RACK_ID = "id-of-tips-96"


def _picked(positions: list[str], at: float) -> OperationRecord:
    return OperationRecord(
        operation=DeviceOperation.PICK_UP_TIPS,
        device_name=_LH,
        affected_labware=[_RACK],
        affected_labware_ids=[_RACK_ID],
        action_id="act-2",
        thread_id="t1",
        details=TipPickUpDetails(
            tip_rack=_RACK, positions=positions, use_channels=None,
        ),
        timestamp=at,
    )


async def _history_with_a_settled_rack() -> OpsHistory:
    """A rack the record has spoken about and a head it has watched, so both
    ledgers read KNOWN before any gap is written."""
    history = OpsHistory(store=memory_ops_store())
    await history.append_initial_state(
        _RACK,
        InitialStateDetails(
            labware=_RACK,
            tip_positions_present=["A1", "B1", "C1"],
            well_volumes=None,
        ),
        labware_id=_RACK_ID,
    )
    await history.append_record(TrackingRecord(
        execution_id=history.execution_id,
        action_id="act-1",
        thread_id="t1",
        method_id=None,
        source=TrackingSource.OBSERVED,
        timestamp=1.0,
        operations=[_picked(["A1"], at=1.0)],
    ))
    return history


@pytest.mark.asyncio
class TestWhichGapsLetAPickThrough:

    async def test_a_restart_is_a_stretch_a_hand_could_have_used(self) -> None:
        history = await _history_with_a_settled_rack()
        await history.append_observation_gap(
            _RACK, ObservationGapCause.RUNTIME_RESTART, labware_id=_RACK_ID,
        )
        ops = await history.ops_for(_RACK)

        assert contents_provenance(ops, _RACK) is Provenance.STALE
        assert went_unobserved(ops, _RACK)

    async def test_a_command_a_stop_cut_off_is_not(self) -> None:
        """The gap a stop writes says a command was cut off with its effect unknown, with
        nobody near the deck. Leniency there would drive a head at a position
        the record calls empty on the chance somebody refilled it."""
        history = await _history_with_a_settled_rack()
        await history.append_observation_gap(
            _RACK, ObservationGapCause.OPERATIONS_DROPPED, labware_id=_RACK_ID,
        )
        ops = await history.ops_for(_RACK)

        assert contents_provenance(ops, _RACK) is Provenance.STALE, (
            "a person still has to look"
        )
        assert not went_unobserved(ops, _RACK), (
            "but the gate that lets a pick through keeps refusing"
        )

    async def test_a_restart_after_a_cut_off_command_does_not_clear_it(
        self,
    ) -> None:
        """The lenient answer is about a rack a hand could have reloaded. A
        restart says nothing about what a cut-off command did, so the rack
        stays refused until somebody counts it."""
        history = await _history_with_a_settled_rack()
        await history.append_observation_gap(
            _RACK, ObservationGapCause.OPERATIONS_DROPPED, labware_id=_RACK_ID,
        )
        await history.append_observation_gap(
            _RACK, ObservationGapCause.RUNTIME_RESTART, labware_id=_RACK_ID,
        )
        ops = await history.ops_for(_RACK)

        assert not went_unobserved(ops, _RACK)

    async def test_counting_the_rack_settles_every_gap_standing_on_it(
        self,
    ) -> None:
        """One statement, two gaps. Clearing them one at a time would leave a
        rack refused forever with nothing an operator could do about it."""
        history = await _history_with_a_settled_rack()
        await history.append_observation_gap(
            _RACK, ObservationGapCause.OPERATIONS_DROPPED, labware_id=_RACK_ID,
        )
        await history.append_observation_gap(
            _RACK, ObservationGapCause.RUNTIME_RESTART, labware_id=_RACK_ID,
        )
        await history.append_set_tip_state(_RACK, ["A1", "B1"], labware_id=_RACK_ID)
        ops = await history.ops_for(_RACK)

        assert not unsettled_gaps(ops, _RACK)
        assert contents_provenance(ops, _RACK) is Provenance.KNOWN
