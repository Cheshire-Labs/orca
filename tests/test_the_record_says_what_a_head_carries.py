"""What a liquid handler's head is carrying is folded from the record.

The driver holds a belief about this and refuses work when it disagrees with
reality; the belief is one orca projected onto it, so the record is the side
that can be checked.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.operations.device import GetMountedTipsOperation
from orca.operations.device_models import (
    GetMountedTipsRequest,
    GetMountedTipsResponse,
    MountedTipDTO,
    MountedTipReadDTO,
    SetMountedTipsRequest,
)
from orca.state.jsonl_store import JsonlOpsHistoryStore
from orca.state.mounted import (
    MalformedTipRecord,
    MountedTip,
    MountedTips,
    MountedTipsLedger,
    fold_mounted,
)
from orca.state.ops_history import OpsHistory
from orca.state.provenance import Provenance
from orca.state.records import (
    DeviceOperation,
    MountedTipsAssertedDetails,
    HeadObservationGapDetails,
    ObservationGapCause,
    OperationRecord,
    TrackingRecord,
    TrackingSource,
    TipDiscardDetails,
    TipDropDetails,
    TipPickUpDetails,
)

_LH = "mlstar_1"


def _op(details, operation: DeviceOperation, device: str = _LH, at: float = 0.0):
    return OperationRecord(
        operation=operation,
        device_name=device,
        affected_labware=[],
        affected_labware_ids=[],
        action_id="a1",
        thread_id="t1",
        details=details,
        timestamp=at,
    )


def _picked(positions: list[str], channels: list[int] | None = None, at: float = 1.0):
    return _op(
        TipPickUpDetails(tip_rack="tips_96", positions=positions, use_channels=channels),
        DeviceOperation.PICK_UP_TIPS, at=at,
    )


class TestNobodyHasSaid:
    def test_an_empty_record_is_unknown_not_empty(self) -> None:
        assert fold_mounted([], _LH).provenance is Provenance.UNKNOWN

    def test_another_devices_pick_says_nothing_about_this_head(self) -> None:
        other = _op(
            TipPickUpDetails(tip_rack="tips_96", positions=["A1"]),
            DeviceOperation.PICK_UP_TIPS, device="mlstar_2",
        )
        assert fold_mounted([other], _LH).provenance is Provenance.UNKNOWN


class TestAPickPutsTipsOnChannels:
    def test_positions_land_on_the_channels_that_took_them(self) -> None:
        mounted = fold_mounted([_picked(["A1", "B1"], [3, 4])], _LH)
        assert mounted.on(3) == MountedTip("tips_96", "A1", channel_is_inferred=False)
        assert mounted.on(4) == MountedTip("tips_96", "B1", channel_is_inferred=False)

    def test_a_record_naming_no_channels_fills_from_zero(self) -> None:
        mounted = fold_mounted([_picked(["A1", "B1"])], _LH)
        assert mounted.on(0) == MountedTip("tips_96", "A1", channel_is_inferred=True)
        assert mounted.on(1) == MountedTip("tips_96", "B1", channel_is_inferred=True)

    def test_a_pick_makes_the_head_known(self) -> None:
        assert fold_mounted([_picked(["A1"])], _LH).provenance is Provenance.KNOWN


class TestWhetherTheChannelNumberWasObserved:
    """The wire carries no channel numbers for tip operations, so a record
    naming none is filed from channel zero. A read that does not say which
    numbers were filled in tells an operator a guess was a measurement."""

    def test_a_pick_that_names_its_channels_is_not_a_guess(self) -> None:
        mounted = fold_mounted([_picked(["A1"], [5])], _LH)
        assert mounted.by_channel[5].channel_is_inferred is False

    def test_a_pick_that_names_none_is_a_guess(self) -> None:
        mounted = fold_mounted([_picked(["A1"])], _LH)
        assert mounted.by_channel[0].channel_is_inferred is True

    def test_an_operator_stating_the_head_is_never_a_guess(self) -> None:
        """set-mounted-tips is the repair for a wrong guess, so what it writes
        cannot come back reading as one."""
        ops = [
            _picked(["A1"]),
            _op(
                MountedTipsAssertedDetails(by_channel={3: ("tips_96", "C1")}),
                DeviceOperation.SET_MOUNTED_TIPS, at=2.0,
            ),
        ]
        mounted = fold_mounted(ops, _LH)
        assert mounted.by_channel[3].channel_is_inferred is False

    def test_one_head_can_hold_both(self) -> None:
        """A guessed channel and an observed one sit side by side, so the
        answer is per channel and not per head."""
        ops = [_picked(["A1"], [4], at=1.0), _picked(["B1"], at=2.0)]
        mounted = fold_mounted(ops, _LH)
        assert mounted.by_channel[4].channel_is_inferred is False
        assert mounted.by_channel[0].channel_is_inferred is True


class TestPuttingThemBack:
    def test_a_drop_clears_the_channels_it_names(self) -> None:
        ops = [
            _picked(["A1", "B1"], [0, 1]),
            _op(
                TipDropDetails(tip_rack="tips_96", positions=["A1"], use_channels=[0]),
                DeviceOperation.DROP_TIPS, at=2.0,
            ),
        ]
        mounted = fold_mounted(ops, _LH)
        assert mounted.on(0) is None
        assert mounted.on(1) == MountedTip("tips_96", "B1", channel_is_inferred=False)

    def test_a_discard_naming_no_channels_empties_the_head(self) -> None:
        """Without its own record a discard left tips mounted forever."""
        ops = [
            _picked(["A1", "B1"], [0, 1]),
            _op(TipDiscardDetails(), DeviceOperation.DISCARD_TIPS, at=2.0),
        ]
        assert fold_mounted(ops, _LH).by_channel == {}

    def test_a_discard_naming_channels_clears_only_those(self) -> None:
        ops = [
            _picked(["A1", "B1"], [0, 1]),
            _op(
                TipDiscardDetails(use_channels=[1]),
                DeviceOperation.DISCARD_TIPS, at=2.0,
            ),
        ]
        assert list(fold_mounted(ops, _LH).by_channel) == [0]


class TestNobodyWasWatching:
    def test_a_gap_makes_the_answer_stale_without_moving_a_tip(self) -> None:
        ops = [
            _picked(["A1"], [0]),
            _op(
                HeadObservationGapDetails(
                    device_name=_LH, cause=ObservationGapCause.RUNTIME_RESTART,
                ),
                DeviceOperation.OBSERVATION_GAP, at=2.0,
            ),
        ]
        mounted = fold_mounted(ops, _LH)
        assert mounted.provenance is Provenance.STALE
        assert mounted.on(0) == MountedTip("tips_96", "A1", channel_is_inferred=False)

    def test_a_tip_operation_after_a_gap_settles_it_again(self) -> None:
        """A head that drops everything after a restart knows what it holds."""
        ops = [
            _picked(["A1"], [0]),
            _op(
                HeadObservationGapDetails(
                    device_name=_LH, cause=ObservationGapCause.RUNTIME_RESTART,
                ),
                DeviceOperation.OBSERVATION_GAP, at=2.0,
            ),
            _op(TipDiscardDetails(), DeviceOperation.DISCARD_TIPS, at=3.0),
        ]
        mounted = fold_mounted(ops, _LH)
        assert mounted.provenance is Provenance.KNOWN
        assert mounted.by_channel == {}
    def test_a_pick_does_not_settle_the_gap_an_abort_left(self) -> None:
        """A pick speaks for the channels it used. Tips an aborted action left
        on the others are still unaccounted for, so the head goes on asking."""
        ops = [
            _picked(["A1"], [0]),
            _op(
                HeadObservationGapDetails(
                    device_name=_LH,
                    cause=ObservationGapCause.OPERATIONS_DROPPED,
                ),
                DeviceOperation.OBSERVATION_GAP, at=2.0,
            ),
            _picked(["A2"], [1], at=3.0),
        ]

        assert fold_mounted(ops, _LH).provenance is Provenance.STALE

    def test_taking_every_tip_off_settles_even_that_one(self) -> None:
        """A discard with no channels named is absolute: nothing is on the head,
        whatever the abort left there."""
        ops = [
            _picked(["A1"], [0]),
            _op(
                HeadObservationGapDetails(
                    device_name=_LH,
                    cause=ObservationGapCause.OPERATIONS_DROPPED,
                ),
                DeviceOperation.OBSERVATION_GAP, at=2.0,
            ),
            _op(TipDiscardDetails(), DeviceOperation.DISCARD_TIPS, at=3.0),
        ]
        mounted = fold_mounted(ops, _LH)

        assert mounted.provenance is Provenance.KNOWN
        assert mounted.by_channel == {}


class TestAnOperatorStatingIt:
    def test_an_assertion_replaces_the_whole_head(self) -> None:
        ops = [
            _picked(["A1", "B1"], [0, 1]),
            _op(
                MountedTipsAssertedDetails(by_channel={2: ("tips_96", "C1")}),
                DeviceOperation.SET_MOUNTED_TIPS, at=2.0,
            ),
        ]
        mounted = fold_mounted(ops, _LH)
        assert mounted.by_channel == {
            2: MountedTip("tips_96", "C1", channel_is_inferred=False)
        }
        assert mounted.provenance is Provenance.KNOWN

    def test_an_empty_assertion_says_the_head_is_bare(self) -> None:
        ops = [
            _picked(["A1"], [0]),
            _op(
                MountedTipsAssertedDetails(by_channel={}),
                DeviceOperation.SET_MOUNTED_TIPS, at=2.0,
            ),
        ]
        mounted = fold_mounted(ops, _LH)
        assert mounted.by_channel == {}
        assert mounted.provenance is Provenance.KNOWN


class TestAPickAcrossTwoRacksKeepsEveryTip:
    """The fold keys on channel. Both records used to count from zero, so the
    second rack's tips were written over the first rack's and half the head
    vanished from the record with nothing saying so."""

    @staticmethod
    def _rack_run(rack: str, positions: list[str], channels: list[int], at: float):
        return _op(
            TipPickUpDetails(
                tip_rack=rack, positions=positions,
                use_channels=channels, channels_were_counted=True,
            ),
            DeviceOperation.PICK_UP_TIPS, at=at,
        )

    def test_four_tips_off_two_racks_are_all_on_the_head(self) -> None:
        ops = [
            self._rack_run("rack_a", ["A1", "B1"], [0, 1], 1.0),
            self._rack_run("rack_b", ["A1", "B1"], [2, 3], 1.0),
        ]
        mounted = fold_mounted(ops, _LH)
        assert mounted.by_channel == {
            0: MountedTip("rack_a", "A1", channel_is_inferred=True),
            1: MountedTip("rack_a", "B1", channel_is_inferred=True),
            2: MountedTip("rack_b", "A1", channel_is_inferred=True),
            3: MountedTip("rack_b", "B1", channel_is_inferred=True),
        }

    def test_counted_channels_are_still_a_guess(self) -> None:
        """The numbers are right relative to each other and still nobody's
        observation, so the read has to keep saying so."""
        mounted = fold_mounted([self._rack_run("rack_a", ["A1"], [7], 1.0)], _LH)
        assert mounted.by_channel[7].channel_is_inferred is True


class TestConfirmingDoesNotTurnAGuessIntoAnObservation:
    """Confirm used to write the fold back through an assertion, and an
    assertion is always stated, so agreeing with a head settled its provenance
    and erased the fact that nobody had ever seen those channel numbers."""

    @staticmethod
    async def _ledger_after_a_guessed_pick() -> MountedTipsLedger:
        history = OpsHistory(JsonlOpsHistoryStore.ephemeral())
        await history.for_execution("exec-1").append_record(TrackingRecord(
            execution_id="exec-1", action_id="a1", thread_id="t1",
            method_id=None, source=TrackingSource.OBSERVED, timestamp=1.0,
            operations=[_picked(["A1", "B1"], at=1.0)],
        ))
        return MountedTipsLedger(history)

    async def test_a_confirm_keeps_the_guess_marked(self) -> None:
        ledger = await self._ledger_after_a_guessed_pick()
        await ledger.note_observation_gap(_LH, ObservationGapCause.RUNTIME_RESTART)

        await ledger.confirm(_LH)

        mounted = await ledger.of(_LH)
        assert mounted.by_channel[0].channel_is_inferred is True
        assert mounted.by_channel[1].channel_is_inferred is True

    async def test_a_confirm_still_settles_the_head(self) -> None:
        ledger = await self._ledger_after_a_guessed_pick()
        await ledger.note_observation_gap(_LH, ObservationGapCause.RUNTIME_RESTART)
        assert (await ledger.of(_LH)).provenance is Provenance.STALE

        await ledger.confirm(_LH)

        assert (await ledger.of(_LH)).provenance is Provenance.KNOWN

    async def test_a_confirm_moves_no_tip(self) -> None:
        ledger = await self._ledger_after_a_guessed_pick()
        before = (await ledger.of(_LH)).by_channel

        await ledger.confirm(_LH)

        after = (await ledger.of(_LH)).by_channel
        assert after == before

    async def test_an_operator_stating_the_head_still_clears_the_guess(self) -> None:
        ledger = await self._ledger_after_a_guessed_pick()

        await ledger.assert_mounted(_LH, {0: ("tips_96", "A1")})

        mounted = await ledger.of(_LH)
        assert mounted.by_channel[0].channel_is_inferred is False


class TestAReadEntryCanBePostedBack:
    """set-mounted-tips is the repair the docs name for a wrong channel number,
    so a read entry has to survive the round trip. The read model's extra field
    used to be rejected by the write model's `extra="forbid"`."""

    def test_a_read_entry_is_accepted_by_the_write_model(self) -> None:
        read = MountedTipReadDTO(
            channel=0, tip_rack="tips_96", position="A1", channel_is_inferred=True,
        )
        request = SetMountedTipsRequest(
            device_name=_LH,
            mounted=[MountedTipDTO.model_validate(read.model_dump())],
        )
        assert request.mounted[0].position == "A1"

    def test_the_write_model_does_not_need_the_field(self) -> None:
        entry = MountedTipDTO(channel=0, tip_rack="tips_96", position="A1")
        assert entry.channel_is_inferred is False


class TestAMalformedRecordIsLoud:
    def test_mismatched_channels_and_positions_do_not_silently_drop_tips(self) -> None:
        with pytest.raises(MalformedTipRecord):
            fold_mounted([_picked(["A1", "B1"], [0])], _LH)


class TestTheLedgerReadsEveryExecution:
    """The fold used to read only the system bucket, so on any real deployment
    it answered "nobody has ever said" while the run's picks sat in that
    execution's bucket. Handing `fold_mounted` a list built in the test cannot
    catch that coming back."""

    async def test_a_pick_written_by_a_run_is_read_back(self) -> None:
        history = OpsHistory(JsonlOpsHistoryStore.ephemeral())
        ledger = MountedTipsLedger(history)
        pick = _picked(["A1"], [0], at=1.0)
        await history.for_execution("exec-1").append_record(TrackingRecord(
            execution_id="exec-1", action_id="a1", thread_id="t1",
            method_id=None, source=TrackingSource.OBSERVED, timestamp=1.0,
            operations=[pick],
        ))

        mounted = await ledger.of(_LH)

        assert mounted.on(0) == MountedTip("tips_96", "A1", channel_is_inferred=False)
        assert mounted.provenance is Provenance.KNOWN

    async def test_a_confirm_is_refused_after_an_abort_lost_the_records(
        self,
    ) -> None:
        """Agreeing writes the read back as an operator's word. After an abort
        the head may be carrying tips the read does not know about, so agreeing
        would put that down as checked."""
        history = OpsHistory(JsonlOpsHistoryStore.ephemeral())
        ledger = MountedTipsLedger(history)
        await history.for_execution("exec-1").append_record(TrackingRecord(
            execution_id="exec-1", action_id="a1", thread_id="t1",
            method_id=None, source=TrackingSource.OBSERVED, timestamp=1.0,
            operations=[_picked(["A1"], [0], at=1.0)],
        ))
        await history.append_head_observation_gap(
            _LH, ObservationGapCause.OPERATIONS_DROPPED,
        )

        with pytest.raises(ValueError) as refusal:
            await ledger.confirm(_LH)

        assert "set_mounted_tips" in str(refusal.value)
        assert (await ledger.of(_LH)).provenance is Provenance.STALE

    async def test_picks_from_two_executions_fold_as_one_head(self) -> None:
        history = OpsHistory(JsonlOpsHistoryStore.ephemeral())
        ledger = MountedTipsLedger(history)
        for execution, channel, at in (("exec-1", 0, 1.0), ("exec-2", 1, 2.0)):
            await history.for_execution(execution).append_record(TrackingRecord(
                execution_id=execution, action_id="a1", thread_id="t1",
                method_id=None, source=TrackingSource.OBSERVED, timestamp=at,
                operations=[_picked(["A1"], [channel], at=at)],
            ))

        mounted = await ledger.of(_LH)

        assert sorted(mounted.by_channel) == [0, 1]


class TestTheReadSurfaceCarriesTheSameAnswer:
    """`GetMountedTipsResponse` used to declare `channels_are_inferred = True`
    and nothing ever set it, so a head an operator had just stated read back as
    guessed."""

    @staticmethod
    def _runtime(mounted: MountedTips) -> MagicMock:
        rt = MagicMock()
        rt.devices.get_mounted_tips = AsyncMock(return_value=mounted)
        return rt

    async def test_a_stated_channel_reads_back_as_stated(self) -> None:
        rt = self._runtime(MountedTips(
            {3: MountedTip("tips_96", "A1", channel_is_inferred=False)},
            Provenance.KNOWN,
        ))
        response = await GetMountedTipsOperation(rt).run(
            GetMountedTipsRequest(device_name=_LH)
        )
        assert response.mounted[0].channel_is_inferred is False

    async def test_a_guessed_channel_reads_back_as_guessed(self) -> None:
        rt = self._runtime(MountedTips(
            {0: MountedTip("tips_96", "A1", channel_is_inferred=True)},
            Provenance.KNOWN,
        ))
        response = await GetMountedTipsOperation(rt).run(
            GetMountedTipsRequest(device_name=_LH)
        )
        assert response.mounted[0].channel_is_inferred is True

    def test_the_head_carries_no_blanket_verdict(self) -> None:
        """The one it used to carry was a constant. Per channel is the only
        granularity the fold can honestly answer."""
        assert "channels_are_inferred" not in GetMountedTipsResponse.model_fields
