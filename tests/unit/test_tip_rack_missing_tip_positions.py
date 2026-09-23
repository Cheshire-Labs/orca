"""TipRackInstance.missing_tip_positions() -- the pre-flight ledger check a
pick consults before dispatch. Same ops-history-backed projection as
``can_continue`` (see test_labware_can_continue.py); this pins the check
that names WHICH positions are missing, not just whether the rack is empty.
"""
from unittest.mock import MagicMock

import pytest

from orca.resource_models.labware import PlateInstance, TipRackInstance
from orca.state.records import ObservationGapCause
from orca.state.provenance import Provenance
from orca.state.ops_history import OpsHistory
from tests.test_helpers import bind_ledger
from tests.unit.test_labware_can_continue import _append_pickup, _seed_rack


def _make_fake_rack(name: str = "tips_384") -> MagicMock:
    rack = MagicMock()
    rack.name = name
    rack.model = "hamilton_96_tiprack"
    return rack


class TestMissingTipPositions:
    @pytest.mark.asyncio
    async def test_present_positions_report_nothing_missing(self) -> None:
        history = OpsHistory()
        inst = TipRackInstance(_make_fake_rack(), template_name="tips_384", labware_type="tips_384")
        bind_ledger(inst, history)
        await _seed_rack(history, inst.name, ["A1", "A2", "B1"])

        assert await inst.missing_tip_positions(["A1", "B1"]) == []

    @pytest.mark.asyncio
    async def test_names_only_the_positions_actually_missing(self) -> None:
        history = OpsHistory()
        inst = TipRackInstance(_make_fake_rack(), template_name="tips_384", labware_type="tips_384")
        bind_ledger(inst, history)
        await _seed_rack(history, inst.name, ["A1", "A2"])
        await _append_pickup(history, inst.name, ["A1"])

        assert await inst.missing_tip_positions(["A1", "A2"]) == ["A1"]

    @pytest.mark.asyncio
    async def test_no_baseline_reports_nothing_missing(self) -> None:
        """A rack this route never bound has no baseline to check against --
        the same "nothing said" default `_can_continue_default` uses, not a
        false claim that every position is empty."""
        history = OpsHistory()
        inst = TipRackInstance(_make_fake_rack(), template_name="tips_384", labware_type="tips_384")
        bind_ledger(inst, history)

        assert await inst.missing_tip_positions(["A1"]) == []

    @pytest.mark.asyncio
    async def test_unbound_rack_reports_nothing_missing(self) -> None:
        inst = TipRackInstance(_make_fake_rack(), template_name="tips_384", labware_type="tips_384")

        assert await inst.missing_tip_positions(["A1"]) == []


class TestTipCountPresent:
    """The count an operator needs to tell "advance a column" from "reload
    the rack" once ``missing_tip_positions`` reports a shortfall."""

    @pytest.mark.asyncio
    async def test_counts_the_positions_the_ledger_backs(self) -> None:
        history = OpsHistory()
        inst = TipRackInstance(_make_fake_rack(), template_name="tips_384", labware_type="tips_384")
        bind_ledger(inst, history)
        await _seed_rack(history, inst.name, ["A1", "A2", "B1"])
        await _append_pickup(history, inst.name, ["A1"])

        assert await inst.tip_count_present() == 2

    @pytest.mark.asyncio
    async def test_no_baseline_counts_zero(self) -> None:
        history = OpsHistory()
        inst = TipRackInstance(_make_fake_rack(), template_name="tips_384", labware_type="tips_384")
        bind_ledger(inst, history)

        assert await inst.tip_count_present() == 0


class TestBaseLabwareHasNoTipConcept:
    @pytest.mark.asyncio
    async def test_non_tip_rack_labware_reports_nothing_missing(self) -> None:
        labware = MagicMock()
        labware.name = "plate_1-abc"
        labware.barcode = None
        labware.size_z = 1.0
        plate = PlateInstance(labware, template_name="plate_1", labware_type="plate_1")

        assert await plate.missing_tip_positions(["A1"]) == []
        assert await plate.tip_count_present() == 0


class TestTipReadingProvenance:
    """The refusal built on ``missing_tip_positions`` has to know whether
    anyone has looked at the rack since the last gap; a fold nobody has
    checked is a question, not evidence."""

    @pytest.mark.asyncio
    async def test_a_seeded_rack_reads_known(self) -> None:
        history = OpsHistory()
        inst = TipRackInstance(_make_fake_rack(), template_name="tips_384", labware_type="tips_384")
        bind_ledger(inst, history)
        await _seed_rack(history, inst.name, ["A1", "A2"])

        assert await inst.contents_provenance() is Provenance.KNOWN

    @pytest.mark.asyncio
    async def test_a_restart_leaves_the_reading_stale(self) -> None:
        history = OpsHistory()
        inst = TipRackInstance(_make_fake_rack(), template_name="tips_384", labware_type="tips_384")
        bind_ledger(inst, history)
        await _seed_rack(history, inst.name, ["A1", "A2"])
        await _append_pickup(history, inst.name, ["A1"])
        await inst.note_observation_gap(ObservationGapCause.RUNTIME_RESTART)

        assert await inst.missing_tip_positions(["A1"]) == ["A1"]
        assert await inst.contents_provenance() is Provenance.STALE
        assert await inst.went_unobserved(), (
            "a restart is when a rack gets reloaded by hand, so a pick at a "
            "position the record calls empty is let through"
        )

    @pytest.mark.asyncio
    async def test_an_aborted_action_leaves_the_reading_stale_but_not_lenient(
        self,
    ) -> None:
        """Both gaps make the read stale. Only one of them is a reason to let a
        pick through at a position the record calls empty: a restart is when a
        person reloads a rack by hand, an abort is the machine losing its own
        record of what it did."""
        history = OpsHistory()
        inst = TipRackInstance(_make_fake_rack(), template_name="tips_384", labware_type="tips_384")
        bind_ledger(inst, history)
        await _seed_rack(history, inst.name, ["A1", "A2"])
        await _append_pickup(history, inst.name, ["A1"])
        await inst.note_observation_gap(ObservationGapCause.OPERATIONS_DROPPED)

        assert await inst.contents_provenance() is Provenance.STALE
        assert not await inst.went_unobserved()

        await inst.note_observation_gap(ObservationGapCause.RUNTIME_RESTART)

        assert not await inst.went_unobserved(), (
            "a restart does not bring back the tips the abort discarded"
        )

    @pytest.mark.asyncio
    async def test_a_rack_nothing_ever_described_reads_unknown(self) -> None:
        history = OpsHistory()
        inst = TipRackInstance(_make_fake_rack(), template_name="tips_384", labware_type="tips_384")
        bind_ledger(inst, history)

        assert await inst.contents_provenance() is Provenance.UNKNOWN
