"""A restart leaves everything worth a look, and everything has to be settleable.

A restart makes every labware stale, which is right: nothing was watching while
the runtime was down. But confirm only existed for racks, so
the only way to settle a plate was to restate every well by hand. Every plate on
the deck read "worth a look" forever after the first restart, which is the
prompt nobody reads.

The rule these pin: a labware stale because nobody was watching can be
confirmed, and confirming agrees with exactly what the read surface displayed,
never with a number the operator was not shown.

The two kinds of stale that mean the record is known not to describe the
labware -- an unfinished action holding operations, an aborted one that lost
them -- are refused instead, because agreeing there writes a number that is
already wrong down as checked.
"""

import pytest

from orca.state.provenance import Provenance
from orca.state.records import ObservationGapCause
from orca.runtime.sim_labware import SimPlateTemplate

from tests.test_labware_contents_are_ledger_owned import _PLATE_WELLS, _started


class TestConfirmingAPlateSettlesIt:
    async def test_a_stale_plate_reads_known_once_confirmed(self) -> None:
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("plate_96", confirm=True)
            plate = next(i for i in system.labwares if i.id == snap.id)
            await plate.note_observation_gap(ObservationGapCause.RUNTIME_RESTART)
            assert (await system.labware_contents.of(plate.ref)).provenance is (
                Provenance.STALE
            )

            await runtime.labware.confirm_well_volumes(snap.id, confirm=True)

            assert (await system.labware_contents.of(plate.ref)).provenance is (
                Provenance.KNOWN
            )
        finally:
            await runtime.shutdown()

    async def test_confirming_records_what_the_read_surface_showed(self) -> None:
        """Confirm means "it matches what you told me", so it must never write
        a number the operator was not shown."""
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("plate_96", confirm=True)
            shown = (await runtime.labware.get_well_volumes(snap.id)).volumes

            await runtime.labware.confirm_well_volumes(snap.id, confirm=True)

            after = (await runtime.labware.get_well_volumes(snap.id)).volumes
            assert after == shown
            assert shown == _PLATE_WELLS
        finally:
            await runtime.shutdown()

    async def test_a_labware_with_no_volumes_at_all_is_refused(self) -> None:
        """Agreeing with nothing would assert an empty plate nobody stated.

        An empty map now means exactly "no volume has ever been spoken about",
        because a well drained to zero rides the read. So a tip rack, which has
        a tip baseline and no volumes, is still refused."""
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            try:
                await runtime.labware.confirm_well_volumes(snap.id, confirm=True)
            except ValueError as exc:
                assert "set_well_volumes" in str(exc)
            else:
                raise AssertionError("a labware with no volumes was confirmed")
        finally:
            await runtime.shutdown()


class TestAPlateNobodyDescribedReadsUnknown:
    """`unknown` has to be reachable or the four states are three.

    A plate template that declares no initial state used to write a grid of
    zeros as its opening entry, which reads back as "folded from an unbroken
    record" -- a confident answer about wells nobody ever looked in.
    """

    async def test_an_undeclared_plate_says_nobody_has_said(self) -> None:
        system, runtime = await _started()
        try:
            plate = await SimPlateTemplate("plate_bare").create_instance()
            await plate.enter_record(system.labware_contents)

            contents = await system.labware_contents.of(plate.ref)
            assert contents.provenance is Provenance.UNKNOWN
            assert contents.volumes == {}
        finally:
            await runtime.shutdown()

    async def test_a_declared_plate_still_reads_its_declaration(self) -> None:
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("plate_96", confirm=True)
            plate = next(i for i in system.labwares if i.id == snap.id)
            contents = await system.labware_contents.of(plate.ref)
            assert contents.provenance is Provenance.KNOWN
            assert contents.volumes == _PLATE_WELLS
        finally:
            await runtime.shutdown()


class TestAPlateTheRunEmptiedStillReads:
    """Absent means nobody said. Zero means known to be empty.

    The operator read used the driver's sparse projection, which drops a well
    folded to exactly zero on purpose: silence is what keeps the driver's
    tracker lenient about a well nobody described. Pointed at an operator it
    said the opposite of the truth -- a plate the run had just emptied came back
    blank, and after a restart it read worth-a-look with nothing to confirm.
    """

    async def test_a_drained_well_reads_zero_not_missing(self) -> None:
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("plate_96", confirm=True)
            plate = next(i for i in system.labwares if i.id == snap.id)
            await runtime.labware.set_well_volumes(
                snap.id, {well: 0.0 for well in _PLATE_WELLS},
                reason="the run drew every well down", confirm=True,
            )

            volumes = (await runtime.labware.get_well_volumes(snap.id)).volumes
            assert volumes == {well: 0.0 for well in _PLATE_WELLS}, (
                "an emptied plate must not read as one nobody has described"
            )
            assert (await system.labware_contents.of(plate)).is_known
        finally:
            await runtime.shutdown()

    async def test_an_emptied_plate_can_still_be_confirmed(self) -> None:
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("plate_96", confirm=True)
            plate = next(i for i in system.labwares if i.id == snap.id)
            await runtime.labware.set_well_volumes(
                snap.id, {well: 0.0 for well in _PLATE_WELLS},
                reason="the run drew every well down", confirm=True,
            )
            await plate.note_observation_gap(ObservationGapCause.RUNTIME_RESTART)

            await runtime.labware.confirm_well_volumes(snap.id, confirm=True)

            assert (await system.labware_contents.of(plate)).provenance is (
                Provenance.KNOWN
            )
        finally:
            await runtime.shutdown()

    async def test_a_well_nobody_mentioned_stays_absent(self) -> None:
        """The other half of the pair: silence still reads as silence."""
        system, runtime = await _started()
        try:
            plate = await SimPlateTemplate("plate_bare").create_instance()
            await plate.enter_record(system.labware_contents)
            assert (await system.labware_contents.of(plate)).volumes == {}
        finally:
            await runtime.shutdown()


class TestATypoDoesNotReportSuccess:
    """Marking a position that is not on the rack changed nothing and answered
    200, so an operator who typed `A13` believed the record was repaired."""

    async def test_a_position_the_rack_does_not_have_is_refused(self) -> None:
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            with pytest.raises(Exception, match="A13"):
                await runtime.labware.mark_tips_used(
                    snap.id, ["A13"], reason="typo", confirm=True,
                )
        finally:
            await runtime.shutdown()

    async def test_a_position_the_rack_has_still_works(self) -> None:
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            remaining = await runtime.labware.mark_tips_used(
                snap.id, ["A1"], reason="a hand took it", confirm=True,
            )
            assert "A1" not in remaining
        finally:
            await runtime.shutdown()


class TestAConfirmTheRecordCannotBack:
    """Confirming is agreeing. Two states leave nothing worth agreeing with."""

    async def test_a_plate_an_abort_touched_refuses_the_confirm(self) -> None:
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("plate_96", confirm=True)
            await runtime.labware.set_well_volumes(
                snap.id, {well: 100.0 for well in _PLATE_WELLS},
                reason="filled at the bench", confirm=True,
            )
            plate = next(i for i in system.labwares if i.id == snap.id)
            await plate.note_observation_gap(
                ObservationGapCause.OPERATIONS_DROPPED,
            )

            with pytest.raises(ValueError) as refusal:
                await runtime.labware.confirm_well_volumes(snap.id, confirm=True)

            assert "set_well_volumes" in str(refusal.value)
            assert (await system.labware_contents.of(plate.ref)).provenance is (
                Provenance.STALE
            ), "the refusal left the read asking to be looked at"
        finally:
            await runtime.shutdown()

    async def test_a_rack_an_abort_touched_refuses_the_confirm(self) -> None:
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            rack = next(i for i in system.labwares if i.id == snap.id)
            await rack.note_observation_gap(
                ObservationGapCause.OPERATIONS_DROPPED,
            )

            with pytest.raises(ValueError) as refusal:
                await runtime.labware.confirm_tip_state(snap.id, confirm=True)

            assert "set_tip_state" in str(refusal.value)
        finally:
            await runtime.shutdown()
