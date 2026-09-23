"""An error pause leaves the labware's contents needing a look.

A paused thread is exactly when a person reaches into the deck: that is what the
pause is FOR. So the stretch between the pause and the operator's decision is
time nothing was watching, which is what an observation gap records.

`ObservationGapCause.ERROR_PAUSE` existed and the knowledge base promised it,
but nothing wrote one -- only a runtime restart and a device reconnect did. A
rack picked from, then paused on, then reached into, read as known and settled,
so no surface asked anyone to check it.

Written at the ledger seam rather than by driving a real thread to failure: the
pause path is slow and hard to provoke, and what is being pinned is that the
cause reaches the record and moves the provenance, not how a thread gets there.
"""

import pytest

from orca.state.provenance import Provenance
from orca.state.records import ObservationGapCause

from tests.test_labware_contents_are_ledger_owned import _started

pytestmark = pytest.mark.asyncio


async def _rack_with_a_baseline():
    """A registered rack: the operator route, which seeds and binds."""
    system, runtime = await _started()
    snap = await runtime.labware.register("tips_96", confirm=True)
    rack = next(i for i in system.labwares if i.id == snap.id)
    return system.labware_contents, rack, runtime


class TestAnErrorPauseIsAnObservationGap:
    async def test_a_paused_thread_leaves_its_labware_worth_a_look(self) -> None:
        ledger, rack, _runtime = await _rack_with_a_baseline()
        assert (await ledger.of(rack.ref)).provenance is Provenance.KNOWN

        await rack.note_observation_gap(ObservationGapCause.ERROR_PAUSE)

        contents = await ledger.of(rack.ref)
        assert contents.provenance is Provenance.STALE, (
            "an error pause is when a hand goes in; nothing asked anyone to look"
        )

    async def test_the_fold_itself_is_untouched(self) -> None:
        """A gap expires an attestation. It moves no tip and no volume."""
        ledger, rack, _runtime = await _rack_with_a_baseline()
        before = (await ledger.of(rack.ref)).tip_positions_present

        await rack.note_observation_gap(ObservationGapCause.ERROR_PAUSE)

        assert (await ledger.of(rack.ref)).tip_positions_present == before

    async def test_an_operator_confirming_settles_it_again(self) -> None:
        ledger, rack, _runtime = await _rack_with_a_baseline()
        await rack.note_observation_gap(ObservationGapCause.ERROR_PAUSE)

        present = (await ledger.of(rack.ref)).tip_positions_present
        await ledger.assert_tips(rack.ref, present)

        assert (await ledger.of(rack.ref)).provenance is Provenance.KNOWN


class TestAnUnboundLabwareDoesNotBlockThePause:
    async def test_reporting_a_gap_on_an_unbound_labware_is_silent(self) -> None:
        """Unlike a read, which raises. Refusing to pause a thread because a
        labware was never wired up would trade a recoverable stop for an
        unrecoverable one, and the gap is only ever a courtesy to a later
        reader."""
        system, _runtime = await _started()
        template = system.get_labware_template("tips_96")
        unbound = await template.create_instance()
        await unbound.note_observation_gap(ObservationGapCause.ERROR_PAUSE)
