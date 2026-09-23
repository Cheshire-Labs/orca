"""An incident about contents must not describe a gripper move.

`_deck_conflict_message` matches on the reason and falls through to an
interrupted-move sentence for anything it does not recognise. A contents
disagreement had no branch, so the operator was told "a gripper move of 'x' did
not finish ... it was going to 'None'" about a rack that had not moved at all.

The words saying what the two sides each claim went the same way: the conflict
carried them and the incident record had nowhere to put them, so they were built
and dropped.
"""

import pytest

from orca.runtime.incident_store import DeckReconcileConflictDetail
from orca.runtime.system_runtime import _deck_conflict_message
from orca.system.system_interface import (
    DeckConflictReason,
    DeckReconcileConflict,
)


def _conflict(reason: DeckConflictReason, detail: str | None = None):
    return DeckReconcileConflict(
        device_name="lh",
        labware_id="id-1",
        labware_name="tips_96",
        position_id="carrier-7-0",
        reason=reason,
        detail=detail,
    )


class TestTheMessageMatchesTheReason:
    def test_a_contents_disagreement_is_not_described_as_a_move(self) -> None:
        message = _deck_conflict_message(
            _conflict(
                DeckConflictReason.CONTENTS_DIFFER,
                "the record has 4 tips, the driver reports 96.",
            )
        )
        assert "gripper move" not in message
        assert "did not finish" not in message

    def test_it_says_what_the_two_sides_claim(self) -> None:
        message = _deck_conflict_message(
            _conflict(
                DeckConflictReason.CONTENTS_DIFFER,
                "the record has 4 tips, the driver reports 96.",
            )
        )
        assert "the record has 4 tips, the driver reports 96." in message

    def test_it_names_a_verb_that_settles_contents(self) -> None:
        """edit-labware-location settles a placement, not a tip count."""
        message = _deck_conflict_message(
            _conflict(DeckConflictReason.CONTENTS_DIFFER, "they differ.")
        )
        assert "set-tip-state" in message
        assert "edit-labware-location" not in message

    def test_a_missing_detail_still_reads(self) -> None:
        message = _deck_conflict_message(
            _conflict(DeckConflictReason.CONTENTS_DIFFER)
        )
        assert "None" not in message

    @pytest.mark.parametrize("reason", list(DeckConflictReason))
    def test_every_reason_has_its_own_sentence(self, reason) -> None:
        """The fall-through is the interrupted-move text, so any reason without
        a branch of its own is silently described as a failed move."""
        message = _deck_conflict_message(_conflict(reason, "they differ."))
        if reason is DeckConflictReason.INTERRUPTED_MOVE:
            return
        assert "did not finish" not in message, (
            f"{reason.name} falls through to the interrupted-move sentence"
        )


class TestTheDetailReachesTheIncidentRecord:
    def test_the_record_carries_what_the_two_sides_claim(self) -> None:
        detail = DeckReconcileConflictDetail(
            device_name="lh",
            labware_id="id-1",
            labware_name="tips_96",
            position_id="carrier-7-0",
            reason=DeckConflictReason.CONTENTS_DIFFER.value,
            detail="the record has 4 tips, the driver reports 96.",
        )
        assert detail.detail == "the record has 4 tips, the driver reports 96."
