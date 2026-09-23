"""The worklist is the short list of things an operator has to settle.

It is only useful if it holds what needs a person and nothing else. An ordinary
destination plate whose template declares no starting volumes is not unsettled:
nobody described it because there was nothing to describe.
"""

from types import SimpleNamespace

import pytest

from orca.runtime.sim_labware import SimPlateTemplate
from orca.state.provenance import Provenance
from orca.state.records import (
    DeviceOperation,
    InitialStateDetails,
    ObservationGapCause,
    OperationRecord,
    TipPickUpDetails,
)

from tests.test_labware_contents_are_ledger_owned import _started


class _ActionHoldingOperations:
    """An action that has done things and has not been folded yet."""

    def __init__(self, operations: list[OperationRecord]) -> None:
        self._operations = operations

    @property
    def pending_operations(self) -> list[OperationRecord]:
        return self._operations


def _a_pick_nobody_recorded(labware_name: str, device_name: str) -> OperationRecord:
    return OperationRecord(
        operation=DeviceOperation.PICK_UP_TIPS,
        device_name=device_name,
        affected_labware=[labware_name],
        affected_labware_ids=[],
        action_id="act-held",
        thread_id="t1",
        details=TipPickUpDetails(
            tip_rack=labware_name, positions=["A1"], use_channels=None,
        ),
        timestamp=1.0,
    )


class TestWhatDoesNotBelongOnIt:
    async def test_a_fresh_undeclared_plate_is_not_unsettled(self) -> None:
        system, runtime = await _started()
        try:
            plate = await SimPlateTemplate("plate_bare").create_instance()
            await plate.enter_record(system.labware_contents)
            system.add_labware(plate)

            subjects = [item.subject for item in await runtime.unsettled_state()]
            assert "plate_bare" not in subjects
        finally:
            await runtime.shutdown()


class TestWhatBelongsOnIt:
    async def test_a_labware_that_went_unwatched_is_listed(self) -> None:
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            instance = next(l for l in system.labwares if l.id == snap.id)
            await instance.note_observation_gap(ObservationGapCause.RUNTIME_RESTART)

            listed = {item.subject: item for item in await runtime.unsettled_state()}
            assert instance.name in listed
            assert listed[instance.name].provenance is Provenance.STALE
        finally:
            await runtime.shutdown()

    async def test_a_rack_is_told_to_use_the_verb_that_accepts_it(self) -> None:
        """`set-well-volumes` refuses a rack and `set-tip-state` refuses a
        plate, so naming one verb for both sent operators at a refusal. This
        rack went unwatched and nothing else, so the cheap verb is right:
        the noun follows the labware, the verb follows how it got here."""
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            instance = next(l for l in system.labwares if l.id == snap.id)
            await instance.note_observation_gap(ObservationGapCause.RUNTIME_RESTART)

            listed = {item.subject: item for item in await runtime.unsettled_state()}
            assert listed[instance.name].settle_with == "confirm-tip-state"
        finally:
            await runtime.shutdown()


class TestTheHalfThatMatters:
    """Hiding a fresh plate is the visible half; hiding a plate that was used
    and then lost its record is the failure, and nothing was pinning it."""

    async def test_a_plate_something_happened_to_is_listed(self) -> None:
        system, runtime = await _started()
        try:
            plate = await SimPlateTemplate("plate_bare").create_instance()
            await plate.enter_record(system.labware_contents)
            system.add_labware(plate)
            await system.labware_contents.assert_volumes(plate, {"A1": 100.0})
            await plate.note_observation_gap(ObservationGapCause.RUNTIME_RESTART)

            listed = {item.subject: item for item in await runtime.unsettled_state()}
            assert plate.name in listed
            assert listed[plate.name].settle_with == "confirm-well-volumes"
        finally:
            await runtime.shutdown()

    async def test_a_head_nobody_has_described_is_listed(self) -> None:
        """The fixture topology has no liquid handler, so the head branch is
        driven directly. Without this nothing covers a head reaching the list
        at all."""
        system, runtime = await _started()
        real = runtime._liquid_handlers
        try:
            runtime._liquid_handlers = lambda: [SimpleNamespace(name="mlstar_1")]
            heads = [item for item in await runtime.unsettled_state()
                     if item.subject_kind == "device_head"]
        finally:
            runtime._liquid_handlers = real
            await runtime.shutdown()

        assert [item.subject for item in heads] == ["mlstar_1"]
        assert heads[0].settle_with == "set-mounted-tips"


class TestWhatEachRowTellsAPersonToDo:
    """A row that sends an operator to the wrong verb is worse than no row: the
    two verbs the confirm family offers write a wrong number down as checked."""

    async def test_a_plate_an_abort_touched_is_told_to_state_not_confirm(
        self,
    ) -> None:
        system, runtime = await _started()
        try:
            plate = await SimPlateTemplate("plate_bare").create_instance()
            await plate.enter_record(system.labware_contents)
            system.add_labware(plate)
            await system.labware_contents.assert_volumes(plate, {"A1": 100.0})
            await plate.note_observation_gap(
                ObservationGapCause.OPERATIONS_DROPPED,
            )

            listed = {item.subject: item for item in await runtime.unsettled_state()}

            detail = listed[plate.name].detail
            assert "aborted" in detail
            assert "set-well-volumes" in detail
            assert "stretch passed with nobody watching" not in detail
        finally:
            await runtime.shutdown()

    async def test_a_head_an_abort_touched_is_named_its_own_verb(self) -> None:
        system, runtime = await _started()
        real = runtime._liquid_handlers
        try:
            await system.mounted_tips.assert_mounted(
                "mlstar_1", {0: ("tips_96", "A1")},
            )
            await system.mounted_tips.note_observation_gap(
                "mlstar_1", ObservationGapCause.OPERATIONS_DROPPED,
            )
            runtime._liquid_handlers = lambda: [SimpleNamespace(name="mlstar_1")]
            heads = [item for item in await runtime.unsettled_state()
                     if item.subject_kind == "device_head"]
        finally:
            runtime._liquid_handlers = real
            await runtime.shutdown()

        assert "aborted" in heads[0].detail
        assert "set-mounted-tips" in heads[0].detail
        assert "count it" not in heads[0].detail, (
            "a head has no count; it carries a tip per channel"
        )


class TestARowCanBeActedOnAsItStands:
    """The verbs take an id and the row names a labware. Without the id a
    client has to look the name up, and two live instances may share one."""

    async def test_a_labware_row_carries_the_id_its_verb_takes(self) -> None:
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            instance = next(l for l in system.labwares if l.id == snap.id)
            await instance.note_observation_gap(ObservationGapCause.RUNTIME_RESTART)

            listed = {item.subject: item for item in await runtime.unsettled_state()}

            assert listed[instance.name].subject_id == instance.id
        finally:
            await runtime.shutdown()

    async def test_a_head_row_carries_no_id_because_its_verb_wants_the_name(
        self,
    ) -> None:
        system, runtime = await _started()
        real = runtime._liquid_handlers
        try:
            runtime._liquid_handlers = lambda: [SimpleNamespace(name="mlstar_1")]
            heads = [item for item in await runtime.unsettled_state()
                     if item.subject_kind == "device_head"]
        finally:
            runtime._liquid_handlers = real
            await runtime.shutdown()

        assert heads[0].subject_id is None


class TestTheVerbTheRowNamesIsOneThatWorks:
    """A row naming a call that errors is worse than a row naming none, so
    every row is checked against what its own verb would actually do."""

    async def test_a_row_never_names_a_confirm_the_record_cannot_back(
        self,
    ) -> None:
        """An all-zero opening entry is a contents baseline, so the provenance
        is not UNKNOWN, but the volumes still fold to nothing and the confirm
        verb refuses. An undeclared trough is seeded exactly this way. Reading
        the provenance alone named `confirm-well-volumes`, which errors."""
        system, runtime = await _started()
        try:
            plate = await SimPlateTemplate("plate_zero").create_instance()
            await plate.enter_record(system.labware_contents)
            system.add_labware(plate)
            await system.ops_history.append_initial_state(
                plate.name,
                InitialStateDetails(
                    labware=plate.name,
                    well_volumes={"A1": 0.0},
                    tip_positions_present=None,
                ),
                labware_id=plate.id,
            )
            await plate.note_observation_gap(ObservationGapCause.RUNTIME_RESTART)

            listed = {item.subject: item for item in await runtime.unsettled_state()}
            row = listed[plate.name]

            assert row.provenance is Provenance.STALE, (
                "the row has to look agreeable, or it is not this bug"
            )
            assert row.settle_with == "set-well-volumes"
            with pytest.raises(ValueError, match="nothing to confirm"):
                await runtime.labware.confirm_well_volumes(plate.id, confirm=True)
        finally:
            await runtime.shutdown()

    async def test_a_contradicted_rack_can_still_be_agreed_with(self) -> None:
        """A command proved the fold wrong at one position, and the pick that
        proved it is folded, so an operator who looks can find the record right
        and say so. That is the contract every other stale row has, and the
        knowledge base has always offered both verbs here."""
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            instance = next(l for l in system.labwares if l.id == snap.id)
            await instance.note_observation_gap(
                ObservationGapCause.OPERATOR_CONTRADICTED,
            )

            listed = {item.subject: item for item in await runtime.unsettled_state()}

            assert listed[instance.name].settle_with == "confirm-tip-state"
            await runtime.labware.confirm_tip_state(instance.id, confirm=True)
        finally:
            await runtime.shutdown()

    async def test_a_rack_an_abort_touched_is_told_to_state_and_confirm_refuses(
        self,
    ) -> None:
        """The abort is the one gap looking cannot settle by agreement: work
        really happened and nothing will ever record it."""
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            instance = next(l for l in system.labwares if l.id == snap.id)
            await instance.note_observation_gap(
                ObservationGapCause.OPERATIONS_DROPPED,
            )

            listed = {item.subject: item for item in await runtime.unsettled_state()}

            assert listed[instance.name].settle_with == "set-tip-state"
            with pytest.raises(ValueError, match="aborted action lost work"):
                await runtime.labware.confirm_tip_state(instance.id, confirm=True)
        finally:
            await runtime.shutdown()


class TestTheCaseNoVerbAnswers:
    """An unfinished action is holding operations the record has not heard
    about. Confirming freezes a number that is behind; stating one gets the
    action's own operations folded on top of it. Only ending the action helps.
    """

    async def test_a_labware_an_unfinished_action_holds_names_no_verb(self) -> None:
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            instance = next(l for l in system.labwares if l.id == snap.id)
            # Strong reference: the registry holds actions weakly.
            action = _ActionHoldingOperations(
                [_a_pick_nobody_recorded(instance.name, "lh_1")],
            )
            system.ops_history.unrecorded.watch("act-held", action)

            listed = {item.subject: item for item in await runtime.unsettled_state()}
            row = listed[instance.name]

            assert row.settle_with is None
            assert "settle the action" in row.detail
        finally:
            await runtime.shutdown()
