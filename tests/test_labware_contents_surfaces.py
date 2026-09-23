"""The operator-facing side of ledger-owned contents.

How well the record knows a labware (provenance), the one call that answers
"what does it hold" with every layer's reading attached, choosing tips a rack
actually has, and the subtraction an operator makes when a pick finds air.

Shares the system builder with ``test_labware_contents_are_ledger_owned``,
which pins the engine behaviour underneath these surfaces.
"""

import time

import pytest

from orca.resource_models.labware import TipRackInstance
from orca.state.contents import ContentsUnknown, NotEnoughTips
from orca.state.provenance import Provenance, Source
from orca.state.ops_history import OpsHistory
from orca.state.records import (
    DeviceOperation,
    InitialStateDetails,
    OperationRecord,
    TrackingRecord,
    TrackingSource,
)
from orca.state.jsonl_store import JsonlOpsHistoryStore
from orca.system.system_interface import ISystem
from tests.test_labware_contents_are_ledger_owned import (
    _COLUMN_1,
    _RACK_POSITIONS,
    _RowMintingStore,
    _record_pick_up,
    _started,
)


async def _record_driver_report(
    system: ISystem, name: str, positions: list[str],
    execution_id: str = "exec_1",
) -> None:
    """Write what a driver reported about the rack: a witness, never a baseline."""
    history: OpsHistory = system.ops_history.for_execution(execution_id)
    now = time.time()
    await history.append_record(TrackingRecord(
        execution_id=execution_id, action_id="a", thread_id="t", method_id=None,
        source=TrackingSource.OBSERVED, timestamp=now,
        operations=[OperationRecord(
            operation=DeviceOperation.PICK_UP_TIPS,
            device_name="lh",
            affected_labware=[name],
            action_id="a", thread_id="t",
            details=InitialStateDetails(
                labware=name, tip_positions_present=list(positions),
            ),
            timestamp=now,
            source=TrackingSource.DRIVER_OBSERVED,
        )],
    ))


class TestProvenanceSaysHowWellItKnows:
    """A boolean cannot tell a confirmed empty rack from one nobody described."""

    async def test_an_undescribed_rack_is_unknown_not_empty(self) -> None:
        system, runtime = await _started()
        try:
            template = system.get_labware_template("tips_96")
            instance = await template.create_instance()
            system.add_labware(instance)
            answer = await runtime.labware.resolve_contents(instance.id)
            assert answer.provenance is Provenance.UNKNOWN
            assert answer.tip_count is None
        finally:
            await runtime.shutdown()

    async def test_a_fresh_registration_is_known_and_needs_no_look(self) -> None:
        _system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            answer = await runtime.labware.resolve_contents(snap.id)
            assert answer.provenance is Provenance.KNOWN
        finally:
            await runtime.shutdown()

    async def test_a_restart_makes_it_stale_and_an_operator_clears_it(self) -> None:
        labware_store = _RowMintingStore()
        ops_store = JsonlOpsHistoryStore.ephemeral()

        _system_a, runtime_a = await _started(labware_store, ops_store)
        snap = await runtime_a.labware.register(
            "tips_96", location="pad1", confirm=True,
        )
        await runtime_a.shutdown()

        _system_b, runtime_b = await _started(labware_store, ops_store)
        try:
            answer = await runtime_b.labware.resolve_contents(snap.id)
            assert answer.provenance is Provenance.STALE
            # The number survived the gap; only its standing changed.
            assert sorted(answer.tip_positions_present or []) == sorted(_RACK_POSITIONS)

            await runtime_b.labware.confirm_tip_state(snap.id, confirm=True)
            answer = await runtime_b.labware.resolve_contents(snap.id)
            assert answer.provenance is Provenance.KNOWN
        finally:
            await runtime_b.shutdown()

    async def test_the_source_names_the_layer_that_answered(self) -> None:
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            assert (await runtime.labware.resolve_contents(snap.id)).source is (
                Source.DECLARATION
            )
            await _record_pick_up(system, snap.name, snap.id, _COLUMN_1)
            assert (await runtime.labware.resolve_contents(snap.id)).source is (
                Source.OPERATION
            )
            await runtime.labware.set_tip_state(
                snap.id, ["A2"], reason="counted them", confirm=True,
            )
            assert (await runtime.labware.resolve_contents(snap.id)).source is (
                Source.OPERATOR
            )
        finally:
            await runtime.shutdown()


class TestOneCallAnswersAndShowsWhatWasShadowed:
    async def test_the_layers_report_the_driver_and_the_declaration(self) -> None:
        """The reading a client would otherwise assemble from four endpoints."""
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            await _record_pick_up(system, snap.name, snap.id, _COLUMN_1)
            await _record_driver_report(system, snap.name, ["A2", "B2"])
            answer = await runtime.labware.resolve_contents(snap.id)
            by_layer = {layer.layer: layer for layer in answer.layers}

            assert answer.tip_count == 2
            assert by_layer["ledger"].agrees is True
            assert by_layer["driver"].tip_count == 2
            assert by_layer["driver"].agrees is True
            # The declaration still says four; it is shadowed, and says so.
            assert by_layer["declaration"].tip_count == 4
            assert by_layer["declaration"].agrees is False
        finally:
            await runtime.shutdown()

    async def test_a_disagreeing_driver_is_reported_not_folded(self) -> None:
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            await _record_pick_up(system, snap.name, snap.id, _COLUMN_1)
            await _record_driver_report(system, snap.name, _RACK_POSITIONS)
            answer = await runtime.labware.resolve_contents(snap.id)
            by_layer = {layer.layer: layer for layer in answer.layers}

            # The driver claims a full rack. The record still says two, and the
            # disagreement is stated rather than settled.
            assert answer.tip_count == 2
            assert by_layer["driver"].tip_count == 4
            assert by_layer["driver"].agrees is False
        finally:
            await runtime.shutdown()


class TestTheRackTellsYouWhichTipsToTake:
    """A surviving rack is unusable without this: a hard-coded column runs out."""

    async def test_next_tips_skips_what_the_first_run_took(self) -> None:
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            instance = next(i for i in system.labwares if i.id == snap.id)
            assert isinstance(instance, TipRackInstance)

            assert await instance.next_tips(2) == ["A1", "B1"]
            await _record_pick_up(system, snap.name, snap.id, _COLUMN_1)
            assert await instance.next_tips(2) == ["A2", "B2"]
        finally:
            await runtime.shutdown()

    async def test_asking_for_more_than_the_rack_holds_is_refused(self) -> None:
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            instance = next(i for i in system.labwares if i.id == snap.id)
            assert isinstance(instance, TipRackInstance)
            with pytest.raises(NotEnoughTips):
                await instance.next_tips(len(_RACK_POSITIONS) + 1)
        finally:
            await runtime.shutdown()

    async def test_a_rack_nobody_has_described_is_refused(self) -> None:
        """Better a refusal than a guess that drives the head into air."""
        system, runtime = await _started()
        try:
            template = system.get_labware_template("tips_96")
            instance = await template.create_instance()
            # Bound but never seeded, which is the one shape that reads
            # "nothing has been said". Every supported route seeds at birth, so
            # a rack that reached here another way is what this refuses on.
            instance.bind_contents(system.labware_contents)
            assert isinstance(instance, TipRackInstance)
            with pytest.raises(ContentsUnknown):
                await instance.next_tips(1)
        finally:
            await runtime.shutdown()


class TestMarkingTipsUsedMovesThePickPast:
    async def test_marking_a_column_used_advances_the_next_pick(self) -> None:
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            remaining = await runtime.labware.mark_tips_used(
                snap.id, _COLUMN_1, reason="pick found air", confirm=True,
            )
            assert remaining == ["A2", "B2"]

            instance = next(i for i in system.labwares if i.id == snap.id)
            assert isinstance(instance, TipRackInstance)
            assert await instance.next_tips(2) == ["A2", "B2"]
        finally:
            await runtime.shutdown()

    async def test_marking_leaves_every_other_position_alone(self) -> None:
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            await _record_pick_up(system, snap.name, snap.id, ["A1"])
            await runtime.labware.mark_tips_used(
                snap.id, ["A2"], reason="operator took it", confirm=True,
            )
            state = await runtime.labware.get_tip_state(snap.id)
            assert state.positions_present == ["B1", "B2"]
        finally:
            await runtime.shutdown()
