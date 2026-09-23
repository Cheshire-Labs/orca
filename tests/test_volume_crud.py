"""Operator volume CRUD (Bug-Report-2 item 4): read/set per-well volumes.

The set path writes a SET_VOLUME ledger record (source=OPERATOR, WHAT only),
the fold treats it as an absolute overwrite, seeding becomes ledger-first so
an off-deck set survives to the next placement, and overfill is rejected at
the operation layer. The reason rides the @dangerous audit trail, not the
ledger record.
"""
from tests.test_helpers import bind_ledger, named_for_template
import pytest

from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from orca.state.projections import sparse_volumes
from orca.state.ops_history import OpsHistory
from orca.state.records import (
    DeviceOperation,
    InitialStateDetails,
    OperationDetails,
    OperationRecord,
    SetVolumeDetails,
    TrackingSource,
)
from orca.runtime.danger import ConfirmationRequired
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.sim_labware import SimTroughTemplate
from orca.runtime.system_runtime import SystemRuntime
from tests.test_labware_state_reconciliation import (
    RESERVOIR_SITE,
    _bind_resident,
    _require_resident,
)
from tests.test_system_runtime import _build_simple_system


def _instance_for(system, labware_id):
    return next(i for i in system.labwares if i.id == labware_id)


class TestSetAndGet:
    async def test_set_then_get_reflects_volume(self) -> None:
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            snap = await runtime.labware.register("plate_96", confirm=True)
            await runtime.labware.set_well_volumes(
                snap.id, {"A1": 100.0, "A2": 50.0}, reason="prefilled by operator", confirm=True,
            )
            volumes = (await runtime.labware.get_well_volumes(snap.id)).volumes
            assert volumes == {"A1": 100.0, "A2": 50.0}
        finally:
            await runtime.shutdown()

    async def test_get_is_empty_for_fresh_register(self) -> None:
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            snap = await runtime.labware.register("plate_96", confirm=True)
            assert (await runtime.labware.get_well_volumes(snap.id)).volumes == {}
        finally:
            await runtime.shutdown()

    async def test_set_is_absolute_overwrite_then_get(self) -> None:
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            snap = await runtime.labware.register("plate_96", confirm=True)
            await runtime.labware.set_well_volumes(snap.id, {"A1": 100.0}, reason="r", confirm=True)
            await runtime.labware.set_well_volumes(snap.id, {"A1": 25.0}, reason="r", confirm=True)
            assert (await runtime.labware.get_well_volumes(snap.id)).volumes == {"A1": 25.0}
        finally:
            await runtime.shutdown()


class TestSetWritesOperatorLedgerRecord:
    async def test_set_writes_set_volume_record_source_operator(self) -> None:
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            snap = await runtime.labware.register("plate_96", confirm=True)
            await runtime.labware.set_well_volumes(snap.id, {"A1": 70.0}, reason="r", confirm=True)
            ops = await system.ops_history.ops_for(snap.name)
            set_ops = [o for o in ops if o.operation == DeviceOperation.SET_VOLUME]
            assert len(set_ops) == 1
            assert set_ops[0].source == TrackingSource.OPERATOR
            details = set_ops[0].details
            assert isinstance(details, SetVolumeDetails)
            assert details.well_volumes == {"A1": 70.0}
        finally:
            await runtime.shutdown()


class TestDangerGate:
    async def test_set_without_confirm_raises(self) -> None:
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            snap = await runtime.labware.register("plate_96", confirm=True)
            with pytest.raises(ConfirmationRequired):
                await runtime.labware.set_well_volumes(snap.id, {"A1": 10.0}, reason="r")
        finally:
            await runtime.shutdown()

    async def test_set_without_reason_raises(self) -> None:
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            snap = await runtime.labware.register("plate_96", confirm=True)
            with pytest.raises(ValueError, match="reason"):
                await runtime.labware.set_well_volumes(snap.id, {"A1": 10.0}, confirm=True)
        finally:
            await runtime.shutdown()


class TestCapacityValidation:
    async def test_overfill_rejected(self) -> None:
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            snap = await runtime.labware.register("plate_96", confirm=True)
            # SimWell capacity is 385 uL; 1000 overflows.
            with pytest.raises(ValueError, match="capacity"):
                await runtime.labware.set_well_volumes(snap.id, {"A1": 1000.0}, reason="r", confirm=True)
            # Nothing written on the rejected set.
            assert (await runtime.labware.get_well_volumes(snap.id)).volumes == {}
        finally:
            await runtime.shutdown()

    async def test_at_capacity_accepted(self) -> None:
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            snap = await runtime.labware.register("plate_96", confirm=True)
            await runtime.labware.set_well_volumes(snap.id, {"A1": 385.0}, reason="r", confirm=True)
            assert (await runtime.labware.get_well_volumes(snap.id)).volumes == {"A1": 385.0}
        finally:
            await runtime.shutdown()


class TestLedgerFirstSeeding:
    async def test_an_off_deck_set_reaches_the_next_driver_projection(self) -> None:
        """An off-deck operator set makes the driver well-state projection
        ledger-first: the next placement seeds the driver from the folded
        ledger, not the (empty) template default."""
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            snap = await runtime.labware.register("plate_96", confirm=True)
            await runtime.labware.set_well_volumes(snap.id, {"A1": 120.0}, reason="r", confirm=True)
            instance = _instance_for(system, snap.id)
            template = system.get_labware_template("plate_96")
            well_state = await instance.driver_well_state()
            assert well_state is not None
            assert well_state.volumes == {"A1": 120.0}
        finally:
            await runtime.shutdown()


def _op(operation: DeviceOperation, labware: str, details: OperationDetails) -> OperationRecord:
    return OperationRecord(
        operation=operation, device_name="lh", affected_labware=[labware],
        action_id="a", thread_id="t", details=details, timestamp=0.0,
    )


class TestOperatorOverrideSparse:
    """Regression for the ledger-first seeding hazard: an undeclared labware's
    zero baseline must NOT ride the wire (it would swap the driver to a strict
    tracker at 0 and break the first aspirate). Only operator-set wells, and
    non-zero folded wells, are projected."""

    def test_no_operator_set_returns_none(self) -> None:
        # Default plate/trough seed: every well zero-padded, no operator set.
        ops = [_op(DeviceOperation.INITIAL_STATE, "p",
                   InitialStateDetails(labware="p", well_volumes={"A1": 0.0, "A2": 0.0}))]
        assert sparse_volumes(ops, "p") is None

    def test_operator_set_drops_zero_baseline_co_wells(self) -> None:
        ops = [
            _op(DeviceOperation.INITIAL_STATE, "p",
                InitialStateDetails(labware="p", well_volumes={"A1": 0.0, "A2": 0.0})),
            _op(DeviceOperation.SET_VOLUME, "p", SetVolumeDetails(labware="p", well_volumes={"A2": 50.0})),
        ]
        # Only the operator-set well rides the wire; A1's zero baseline stays lenient.
        assert sparse_volumes(ops, "p") == {"A2": 50.0}

    def test_operator_set_zero_is_kept(self) -> None:
        ops = [
            _op(DeviceOperation.INITIAL_STATE, "tr",
                InitialStateDetails(labware="tr", well_volumes={"A1": 0.0}, single_pool=True)),
            _op(DeviceOperation.SET_VOLUME, "tr", SetVolumeDetails(labware="tr", well_volumes={"A1": 0.0})),
        ]
        # An operator deliberately asserting empty rides the wire (strict-at-0).
        assert sparse_volumes(ops, "tr") == {"A1": 0.0}


class TestDefaultTroughStaysLenient:
    async def test_seeded_default_trough_resolves_to_none(self) -> None:
        """A default (undeclared) trough is seeded {"A1": 0.0} at creation. Its
        driver projection must be None (send nothing -> lenient), not {"A1": 0.0}
        which would make the first aspirate raise on a strict-at-0 tracker."""
        history = OpsHistory()
        template = SimTroughTemplate("trough")
        instance = await template.create_instance()
        await instance.enter_record(bind_ledger(instance, history))
        assert await instance.driver_well_state() is None

    async def test_operator_set_default_trough_resolves_to_value(self) -> None:
        history = OpsHistory()
        template = SimTroughTemplate("trough")
        instance = await template.create_instance()
        await instance.enter_record(bind_ledger(instance, history))
        await history.append_set_volume(instance.name, {"A1": 5000.0})
        ws = await instance.driver_well_state()
        assert ws is not None and ws.volumes == {"A1": 5000.0}


class TestOnDeckPush:
    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_on_deck_set_pushes_well_state_to_driver(self) -> None:
        """A set on an on-deck resident pushes the new volume to the driver
        tracker (well_state wire); ledger and the pushed driver projection
        agree. The push names that one resident: a set is not a reason to
        re-project the rest of the deck."""
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        store = InMemoryLabwareStore()
        build, lh, runtime = await _bind_resident(recorder, store, wf_name="recon_set_volume")
        try:
            resident = _require_resident(build, RESERVOIR_SITE)
            # The run bound this resident in PURE_SIM. An operator write means the
            # real device, so it lands in the LIVE world, which that run never laid
            # out. Bring it up first: narrowness is a claim about a write into a
            # deck whose occupancy is already known.
            await runtime.devices.reconcile_deck(lh.name, confirm=True)
            recorder.calls.clear()
            await runtime.labware.set_well_volumes(
                resident.id, {"A1": 12345.0}, reason="operator top-up", confirm=True,
            )

            assert (await runtime.labware.get_well_volumes(resident.id)).volumes["A1"] == 12345.0

            pushes = [
                c for c in recorder.calls
                if c.method == "add_deck_labware"
                and named_for_template(c.args["name"], "reservoir")
            ]
            assert pushes, (
                f"on-deck set must push the resident to the driver; driver saw "
                f"{[c.method for c in recorder.calls]}"
            )
            assert pushes[-1].args["well_state"]["volumes"] == {"A1": 12345.0}, (
                "driver tracker did not receive the operator-set volume"
            )
            assert not [c for c in recorder.calls if c.method == "reconcile_deck_occupancy"], (
                "a set on one resident rebuilt the whole deck"
            )
        finally:
            await runtime.shutdown()
