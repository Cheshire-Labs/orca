"""can_continue(demand) layered predicate tests.

Resolution order: template.can_continue_fn wins; otherwise
subclass _can_continue_default runs; otherwise base default True.
"""
from unittest.mock import MagicMock

import pytest

from orca.resource_models.labware import (
    LabwareInitialState,
    LabwareInstance,
    PlateInstance,
    TipRackInstance,
)
from tests.test_helpers import (
    bind_ledger,
    FactoryPlateTemplate as PlateTemplate,
    FactoryTipRackTemplate as TipRackTemplate,
)
from orca.state.projections import op_count, tips_present
from orca.state.ops_history import OpsHistory
from orca.state.records import (
    DeclaredTracking,
    DeviceOperation,
    DispenseDetails,
    InitialStateDetails,
    OperationRecord,
    TipPickUpDetails,
)


def _make_fake_rack(num_tips: int = 96, name: str = "rack"):
    rack = MagicMock()
    rack.name = name
    rack.model = "hamilton_96_tiprack"
    rack.num_tips = num_tips
    spots = []
    for r in range(8):
        for c in range(num_tips // 8):
            spot = MagicMock()
            spot.identifier = f"{chr(ord('A') + r)}{c + 1}"
            spots.append(spot)
    rack.tip_spots.return_value = spots
    return rack


class TestTipRackCanContinue:
    @pytest.mark.asyncio
    async def test_fresh_seeded_rack_reports_true(self) -> None:
        history = OpsHistory()
        inst = TipRackInstance(_make_fake_rack(num_tips=2), template_name="rack", labware_type="rack")
        await history.append_initial_state(inst.name, InitialStateDetails(labware=inst.name, tip_positions_present=["A1", "A2"]))
        bind_ledger(inst, history)
        assert await inst.can_continue() is True

    @pytest.mark.asyncio
    async def test_depleted_rack_reports_false(self) -> None:
        history = OpsHistory()
        inst = TipRackInstance(_make_fake_rack(num_tips=2), template_name="rack", labware_type="rack")
        await history.append_initial_state(inst.name, InitialStateDetails(labware=inst.name, tip_positions_present=["A1", "A2"]))
        import time as _time
        from orca.state.records import TrackingRecord, TrackingSource
        t = _time.time()
        await history.append_record(TrackingRecord(
            execution_id="_system",
            action_id="a", thread_id="t", method_id="m",
            source=TrackingSource.OBSERVED, timestamp=t,
            operations=[
                OperationRecord(
                    operation=DeviceOperation.PICK_UP_TIPS,
                    device_name="lh",
                    affected_labware=[inst.name],
                    action_id="a", thread_id="t",
                    details=TipPickUpDetails(tip_rack=inst.name, positions=["A1", "A2"]),
                    timestamp=t,
                ),
            ],
        ))
        bind_ledger(inst, history)
        assert await inst.can_continue() is False

    @pytest.mark.asyncio
    async def test_demand_precheck_fails_when_positions_missing(self) -> None:
        history = OpsHistory()
        inst = TipRackInstance(_make_fake_rack(num_tips=96), template_name="rack", labware_type="rack")
        await history.append_initial_state(inst.name, InitialStateDetails(labware=inst.name, tip_positions_present=["A1", "A2", "A3"]))
        bind_ledger(inst, history)
        demand = DeclaredTracking(tips_used={inst.template_name: ["A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8"]})
        assert await inst.can_continue(demand) is False

    @pytest.mark.asyncio
    async def test_demand_precheck_passes_when_all_positions_present(self) -> None:
        history = OpsHistory()
        inst = TipRackInstance(_make_fake_rack(num_tips=96), template_name="rack", labware_type="rack")
        await history.append_initial_state(inst.name, InitialStateDetails(labware=inst.name, tip_positions_present=["A1", "A2", "A3"]))
        bind_ledger(inst, history)
        demand = DeclaredTracking(tips_used={inst.template_name: ["A1", "A2"]})
        assert await inst.can_continue(demand) is True


class TestTemplateCanContinueFnPriority:
    @pytest.mark.asyncio
    async def test_user_fn_overrides_subclass_default(self) -> None:
        # Subclass default would say True (seeded with positions); user fn forces False.
        history = OpsHistory()

        async def always_false(lw: LabwareInstance, demand: DeclaredTracking | None) -> bool:
            return False

        template = TipRackTemplate("rack", _make_fake_rack, with_tips=True, can_continue_fn=always_false)
        inst = TipRackInstance(_make_fake_rack(), template_name="rack", labware_type="rack")
        inst._template = template
        await history.append_initial_state(inst.name, InitialStateDetails(labware=inst.name, tip_positions_present=["A1"]))
        bind_ledger(inst, history)
        assert await inst.can_continue() is False

    @pytest.mark.asyncio
    async def test_smc_full_after_4_contributions(self) -> None:
        """Canonical user-defined capacity: plate is full after 4 dispenses."""

        async def full_after_4_contributions(lw: LabwareInstance, demand: DeclaredTracking | None) -> bool:
            return op_count(await lw.ops(), lw.name, (DeviceOperation.DISPENSE,)) < 4

        history = OpsHistory()
        template = PlateTemplate(
            "final",
            lambda name, with_lid=None: _make_fake_plate(name),
            can_continue_fn=full_after_4_contributions,
        )
        inst = PlateInstance(_make_fake_plate("final"), template_name="final", labware_type="final")
        inst._template = template
        bind_ledger(inst, history)

        # Zero dispenses: has capacity.
        assert await inst.can_continue() is True

        # Append 3 dispenses: still capacity.
        import time as _time
        from orca.state.records import TrackingRecord, TrackingSource
        for _ in range(3):
            t = _time.time()
            await history.append_record(TrackingRecord(
                execution_id="_system",
                action_id="a", thread_id="t", method_id="m",
                source=TrackingSource.OBSERVED, timestamp=t,
                operations=[
                    OperationRecord(
                        operation=DeviceOperation.DISPENSE,
                        device_name="lh",
                        affected_labware=[inst.name],
                        action_id="a", thread_id="t",
                        details=DispenseDetails(labware=inst.name, positions=["A1"], volumes=[10.0]),
                        timestamp=t,
                    ),
                ],
            ))
        assert await inst.can_continue() is True

        # 4th dispense: capacity exhausted.
        t = _time.time()
        await history.append_record(TrackingRecord(
            execution_id="_system",
            action_id="a4", thread_id="t", method_id="m",
            source=TrackingSource.OBSERVED, timestamp=t,
            operations=[
                OperationRecord(
                    operation=DeviceOperation.DISPENSE,
                    device_name="lh",
                    affected_labware=[inst.name],
                    action_id="a4", thread_id="t",
                    details=DispenseDetails(labware=inst.name, positions=["A1"], volumes=[10.0]),
                    timestamp=t,
                ),
            ],
        ))
        assert await inst.can_continue() is False


def _make_fake_plate(name: str):
    plate = MagicMock()
    plate.name = name
    plate.model = "plate_model"
    plate.barcode = None
    plate.num_rows = 8
    plate.num_cols = 12
    return plate
