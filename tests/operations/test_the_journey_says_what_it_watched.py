"""A labware's journey answers "what happened to this plate?".

Two things stopped it. Every liquid-handler call writes one state snapshot per
labware on the deck, and those snapshots came back as journey steps: half the
plate's entries were snapshots, each carrying every well, and nothing on an
entry said which was which. And a snapshot wore the triggering call's verb, so
the plate's journey showed `pick_up_tips` on an entry whose details were a
96-well volume dump for a pick that happened to the rack.

Entries now carry `source`, snapshots are off by default, and the interpreter
labels a snapshot for what it is (see test_driver_observed_records.py).
"""

from dataclasses import dataclass
from typing import get_args

import pytest

from orca.operations.labware import GetLabwareJourneyOperation
from orca.operations.labware_models import GetLabwareJourneyRequest, JourneyAction
from orca.runtime.status_models import LabwareSnapshot, LocationEvent
from orca.state.records import (
    AspirateDetails,
    DeviceOperation,
    InitialStateDetails,
    OperationRecord,
    TrackingRecord,
    TrackingSource,
)


pytestmark = pytest.mark.asyncio

_NOW = 1_788_192_000.0


def _observed_aspirate(labware: str) -> OperationRecord:
    return OperationRecord(
        operation=DeviceOperation.ASPIRATE,
        device_name="lh_1",
        affected_labware=[labware],
        action_id="act-1",
        thread_id="thr-1",
        details=AspirateDetails(labware=labware, positions=["A1"], volumes=[50.0]),
        timestamp=_NOW,
        source=TrackingSource.OBSERVED,
    )


def _driver_snapshot(labware: str) -> OperationRecord:
    return OperationRecord(
        operation=DeviceOperation.INITIAL_STATE,
        device_name="lh_1",
        affected_labware=[labware],
        action_id="act-1",
        thread_id="thr-1",
        details=InitialStateDetails(labware=labware, well_volumes={"A1": 50.0}),
        timestamp=_NOW + 0.1,
        source=TrackingSource.DRIVER_OBSERVED,
    )


@dataclass
class _FakeLabwareFacade:
    snapshot: LabwareSnapshot
    history: list[LocationEvent]

    async def get_by_id(self, labware_id: str) -> LabwareSnapshot:
        del labware_id
        return self.snapshot

    async def get_history(self, labware_id: str) -> list[LocationEvent]:
        del labware_id
        return self.history


@dataclass
class _FakeOpsHistory:
    record: TrackingRecord

    async def search(self, query: object) -> list[tuple[str, TrackingRecord]]:
        del query
        return [("exec-1", self.record)]


@dataclass
class _FakeRuntime:
    labware: _FakeLabwareFacade
    ops_history: _FakeOpsHistory


def _runtime(labware_name: str = "assay_plate") -> _FakeRuntime:
    record = TrackingRecord(
        execution_id="exec-1",
        action_id="act-1",
        thread_id="thr-1",
        method_id="m-1",
        source=TrackingSource.OBSERVED,
        timestamp=_NOW,
        operations=[_observed_aspirate(labware_name), _driver_snapshot(labware_name)],
    )
    snapshot = LabwareSnapshot(
        id="lw-1", name=labware_name, template_name=labware_name,
        barcode=None, current_location="lh_1",
    )
    return _FakeRuntime(
        labware=_FakeLabwareFacade(
            snapshot=snapshot,
            history=[LocationEvent(sequence=0, position_id="lh_1", timestamp=_NOW - 10)],
        ),
        ops_history=_FakeOpsHistory(record=record),
    )


async def test_a_driver_snapshot_is_not_a_step() -> None:
    op = GetLabwareJourneyOperation(runtime=_runtime())

    result = await op.run(GetLabwareJourneyRequest(labware_id="lw-1"))

    actions = [e for e in result.entries if e.kind == "action"]
    assert [a.operation for a in actions] == ["aspirate"]


async def test_snapshots_come_back_when_asked_for() -> None:
    op = GetLabwareJourneyOperation(runtime=_runtime())

    result = await op.run(
        GetLabwareJourneyRequest(labware_id="lw-1", include_driver_snapshots=True),
    )

    actions = [e for e in result.entries if e.kind == "action"]
    assert [a.source for a in actions] == ["observed", "driver_observed"]


async def test_every_action_entry_says_who_produced_it() -> None:
    op = GetLabwareJourneyOperation(runtime=_runtime())

    result = await op.run(GetLabwareJourneyRequest(labware_id="lw-1"))

    actions = [e for e in result.entries if e.kind == "action"]
    assert actions and all(a.source == "observed" for a in actions)


async def test_a_move_still_sorts_before_the_action_that_followed_it() -> None:
    op = GetLabwareJourneyOperation(runtime=_runtime())

    result = await op.run(GetLabwareJourneyRequest(labware_id="lw-1"))

    assert [e.kind for e in result.entries] == ["move", "action"]


async def test_the_wire_vocabulary_covers_every_tracking_source() -> None:
    """`JourneyAction.source` spells the four out as a Literal so the wire
    contract validates itself. A fifth TrackingSource would otherwise fail
    validation the first time a journey read one, on the operator's screen
    rather than here."""
    declared = set(get_args(JourneyAction.model_fields["source"].annotation))

    assert declared == {s.value for s in TrackingSource}
