"""Verifies TipRackInstance.can_continue() under the ops-history backed projection.

Original intent preserved: the three scenarios that matter for the spawn gate
are a freshly stocked rack (True), a rack emptied by picks (False), and a rack
with some picks but not all (True). Under the t7 design, "fresh" means the
rack has been seeded with an INITIAL_STATE op -- this is what ThreadFactory
does at bootstrap via template.seed_initial_state. Tests construct an
OpsHistory and wire it directly to each instance, mirroring that bootstrap.
"""
import time
from unittest.mock import MagicMock

import pytest

from orca.resource_models.labware import TipRackInstance
from orca.state.ops_history import OpsHistory
from tests.test_helpers import bind_ledger
from orca.state.records import (
    DeviceOperation,
    InitialStateDetails,
    OperationRecord,
    TipPickUpDetails,
    TrackingRecord,
    TrackingSource,
)


def _make_fake_rack(name: str = "tips_384") -> MagicMock:
    rack = MagicMock()
    rack.name = name
    rack.model = "hamilton_96_tiprack"
    return rack


async def _seed_rack(history: OpsHistory, rack_name: str, positions: list[str]) -> None:
    await history.append_initial_state(
        rack_name,
        InitialStateDetails(labware=rack_name, tip_positions_present=positions),
    )


async def _append_pickup(history: OpsHistory, rack_name: str, positions: list[str]) -> None:
    now = time.time()
    await history.append_record(TrackingRecord(
        execution_id="_system",
        action_id=f"pickup-{positions[0]}",
        thread_id="t-test",
        method_id="m-test",
        source=TrackingSource.OBSERVED,
        timestamp=now,
        operations=[
            OperationRecord(
                operation=DeviceOperation.PICK_UP_TIPS,
                device_name="lh",
                affected_labware=[rack_name],
                action_id=f"pickup-{positions[0]}",
                thread_id="t-test",
                details=TipPickUpDetails(tip_rack=rack_name, positions=positions),
                timestamp=now,
            ),
        ],
    ))


class TestTipRackHasCapacity:
    @pytest.mark.asyncio
    async def test_fresh_rack_reports_true(self) -> None:
        history = OpsHistory()
        inst = TipRackInstance(_make_fake_rack(), template_name="tips_384", labware_type="tips_384")
        bind_ledger(inst, history)
        await _seed_rack(history, inst.name, ["A1", "A2"])

        assert await inst.can_continue() is True

    @pytest.mark.asyncio
    async def test_depleted_rack_reports_false(self) -> None:
        history = OpsHistory()
        inst = TipRackInstance(_make_fake_rack(), template_name="tips_384", labware_type="tips_384")
        bind_ledger(inst, history)
        await _seed_rack(history, inst.name, ["A1", "A2"])
        await _append_pickup(history, inst.name, ["A1", "A2"])

        assert await inst.can_continue() is False

    @pytest.mark.asyncio
    async def test_partially_used_rack_reports_true(self) -> None:
        history = OpsHistory()
        inst = TipRackInstance(_make_fake_rack(), template_name="tips_384", labware_type="tips_384")
        bind_ledger(inst, history)
        await _seed_rack(history, inst.name, ["A1", "A2", "B1", "B2"])
        await _append_pickup(history, inst.name, ["A1"])

        assert await inst.can_continue() is True

    @pytest.mark.asyncio
    async def test_rack_declared_empty_reports_false(self) -> None:
        """An INITIAL_STATE naming no positions is a real answer: with_tips=False
        means the rack starts empty and the gate must say so."""
        history = OpsHistory()
        inst = TipRackInstance(_make_fake_rack(), template_name="tips_384", labware_type="tips_384")
        bind_ledger(inst, history)
        await _seed_rack(history, inst.name, [])

        assert await inst.can_continue() is False

    @pytest.mark.asyncio
    async def test_rack_with_no_seed_in_reach_reports_true(self) -> None:
        """A rack restored from a store across a restart: its seed sits in an
        execution bucket nothing has bound, so the fold has no baseline. Empty
        then means "never seen", not "ran out", and the gate must not strand
        a deck resident that is very likely still stocked.
        """
        history = OpsHistory()
        inst = TipRackInstance(_make_fake_rack(), template_name="tips_384", labware_type="tips_384")
        bind_ledger(inst, history)

        assert await inst.can_continue() is True
