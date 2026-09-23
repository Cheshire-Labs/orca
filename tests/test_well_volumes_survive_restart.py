"""Well volumes survive a runtime restart, on the read path and the driver seed.

The gap this pins (state-ownership audit D2/D5, ruled 2026-08-24: seed from
history): the volume fold read through per-instance bound ops sources, which a
restart severs, so `get_well_volumes` answered {} and the driver deck was
re-seeded from the template declaration while the durable ledger held the real
per-well map. The store double here mints identity-only instances per lookup,
like a hosted deployment's DbLabwareStore; the identity-map default made the seam invisible.
"""

import time

from orca.resource_models.labware import LabwareInstance
from orca.state.projections import sparse_volumes
from orca.state.ops_history import OpsHistory
from orca.state.records import (
    AspirateDetails,
    DeviceOperation,
    DispenseDetails,
    InitialStateDetails,
    OperationRecord,
    TrackingRecord,
    TrackingSource,
)
from orca.state.jsonl_store import JsonlOpsHistoryStore
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.system_runtime import SystemRuntime
from tests.test_helpers import bind_ledger
from tests.test_system_runtime import _build_simple_system


class DbShapedLabwareStore(InMemoryLabwareStore):
    """Mints a fresh identity-only instance per lookup, like a hosted deployment's
    DbLabwareStore. The identity-map default returns the same live object,
    which hides every binding seam this file exists to pin."""

    async def get_by_id(self, labware_id: str) -> LabwareInstance | None:
        stored = await super().get_by_id(labware_id)
        if stored is None:
            return None
        return LabwareInstance(
            template_name=stored.template_name,
            labware_type=stored.labware_type,
            barcode=stored.barcode,
            instance_id=stored.id,
            name=stored.name,
        )


def _aspirate_record(labware_name: str, well: str, volume: float) -> TrackingRecord:
    now = time.time()
    return TrackingRecord(
        execution_id="exec_1",
        action_id="a1",
        thread_id="t1",
        method_id=None,
        source=TrackingSource.DECLARED,
        timestamp=now,
        operations=[
            OperationRecord(
                operation=DeviceOperation.ASPIRATE,
                device_name="lh",
                affected_labware=[labware_name],
                action_id="a1",
                thread_id="t1",
                details=AspirateDetails(
                    labware=labware_name, positions=[well], volumes=[volume],
                ),
                timestamp=now,
            )
        ],
    )


class TestVolumesSurviveRestart:
    async def _restarted_runtime_with_history(self):
        """Run A: register + operator-set volumes. Run B: fresh system, same
        stores. Returns (system_b, runtime_b, labware_id)."""
        labware_store = DbShapedLabwareStore()
        ops_store = JsonlOpsHistoryStore.ephemeral()

        system_a, _ = await _build_simple_system()
        system_a.ops_history.bind_store(ops_store)
        runtime_a = SystemRuntime(system_a, labware_store=labware_store)
        await runtime_a.start()
        snap = await runtime_a.labware.register(
            "plate_96", location="pad1", confirm=True,
        )
        await runtime_a.labware.set_well_volumes(
            snap.id, {"A1": 120.0}, reason="prefilled", confirm=True,
        )
        await runtime_a.shutdown()

        system_b, _ = await _build_simple_system()
        system_b.ops_history.bind_store(ops_store)
        runtime_b = SystemRuntime(system_b, labware_store=labware_store)
        await runtime_b.start()
        return system_b, runtime_b, snap.id

    async def test_get_well_volumes_reads_the_ledger_after_restart(self) -> None:
        system_b, runtime_b, labware_id = await self._restarted_runtime_with_history()
        try:
            assert (await runtime_b.labware.get_well_volumes(labware_id)).volumes == {"A1": 120.0}
        finally:
            await runtime_b.shutdown()

    async def test_driver_seed_folds_the_ledger_after_restart(self) -> None:
        system_b, runtime_b, labware_id = await self._restarted_runtime_with_history()
        try:
            instance = next(i for i in system_b.labwares if i.id == labware_id)
            template = system_b.get_labware_template("plate_96")
            state = await instance.driver_well_state()
            assert state is not None
            assert state.volumes == {"A1": 120.0}
        finally:
            await runtime_b.shutdown()


class TestSuccessorDoesNotInheritNamesakeVolumes:
    """PLR-backed residents reuse a fixed name every boot, and retire keeps
    ops rows, so the fold must scope by instance id: a fresh same-named
    successor must not inherit the dead labware's volumes."""

    async def _boot(self, labware_store, ops_store):
        system, _ = await _build_simple_system()
        system.ops_history.bind_store(ops_store)
        runtime = SystemRuntime(system, labware_store=labware_store)
        await runtime.start()
        return system, runtime

    async def test_fresh_same_named_instance_reads_empty_volumes(self) -> None:
        labware_store = DbShapedLabwareStore()
        ops_store = JsonlOpsHistoryStore.ephemeral()

        system_a, runtime_a = await self._boot(labware_store, ops_store)
        dead = LabwareInstance("plate_96", "96_well", name="resident_trough")
        system_a.add_labware(dead)
        await labware_store.register(dead)
        await system_a.ops_history.append_set_volume(
            dead.name, {"A1": 120.0}, dead.id,
        )
        assert (await runtime_a.labware.get_well_volumes(dead.id)).volumes == {"A1": 120.0}
        await runtime_a.shutdown()

        system_b, runtime_b = await self._boot(labware_store, ops_store)
        successor = LabwareInstance("plate_96", "96_well", name="resident_trough")
        system_b.add_labware(successor)
        await labware_store.register(successor)
        try:
            assert (await runtime_b.labware.get_well_volumes(successor.id)).volumes == {}
        finally:
            await runtime_b.shutdown()

    async def test_rehydrated_successor_driver_seed_ignores_namesake_history(self) -> None:
        labware_store = DbShapedLabwareStore()
        ops_store = JsonlOpsHistoryStore.ephemeral()

        system_a, runtime_a = await self._boot(labware_store, ops_store)
        dead = LabwareInstance("plate_96", "96_well", name="resident_trough")
        await labware_store.register(dead)
        await system_a.ops_history.append_set_volume(
            dead.name, {"A1": 120.0}, dead.id,
        )
        await runtime_a.shutdown()

        successor = LabwareInstance("plate_96", "96_well", name="resident_trough")
        await labware_store.register(successor)
        await labware_store.update_location(successor.id, "pad1")
        system_b, runtime_b = await self._boot(labware_store, ops_store)
        try:
            instance = next(i for i in system_b.labwares if i.id == successor.id)
            template = system_b.get_labware_template("plate_96")
            state = await instance.driver_well_state()
            seeded = {} if state is None or state.volumes is None else state.volumes
            assert seeded.get("A1") != 120.0
        finally:
            await runtime_b.shutdown()


class TestPipettedHistorySeeds:
    async def test_pipetted_plate_seeds_current_volumes_without_operator_set(self) -> None:
        """A plate that was aspirated from must re-seed at its folded current
        volumes; requiring an operator SET_VOLUME made every reconcile of a
        merely-pipetted plate reset it to the declared initial state."""
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            snap = await runtime.labware.register("plate_96", confirm=True)
            instance = next(i for i in system.labwares if i.id == snap.id)
            history = OpsHistory(store=system.ops_history.store, execution_id="exec_1")
            await history.append_initial_state(
                instance.name,
                InitialStateDetails(
                    labware=instance.name, well_volumes={"A1": 100.0, "A2": 100.0},
                ),
            )
            await history.append_record(_aspirate_record(instance.name, "A1", 40.0))
            bind_ledger(instance, history)

            template = system.get_labware_template("plate_96")
            state = await instance.driver_well_state()
            assert state is not None
            assert state.volumes == {"A1": 60.0, "A2": 100.0}
        finally:
            await runtime.shutdown()


class TestVolumesSurviveDischarge:
    async def test_discharged_plate_volumes_stay_queryable(self) -> None:
        """Discharge is a lifecycle end, not a retraction: the run's dispense
        record must stay readable by the plate's id after the plate leaves the
        deck, in the same session, with no restart in between."""
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            snap = await runtime.labware.register(
                "plate_96", location="pad1", confirm=True,
            )
            instance = next(i for i in system.labwares if i.id == snap.id)
            history = OpsHistory(store=system.ops_history.store, execution_id="exec_1")
            await history.append_initial_state(
                instance.name,
                InitialStateDetails(
                    labware=instance.name, well_volumes={"A1": 0.0, "B1": 0.0},
                ),
            )
            now = time.time()
            await history.append_record(TrackingRecord(
                execution_id="exec_1",
                action_id="a1",
                thread_id="t1",
                method_id=None,
                source=TrackingSource.OBSERVED,
                timestamp=now,
                operations=[
                    OperationRecord(
                        operation=DeviceOperation.DISPENSE,
                        device_name="lh",
                        affected_labware=[instance.name],
                        action_id="a1",
                        thread_id="t1",
                        details=DispenseDetails(
                            labware=instance.name,
                            positions=["A1", "B1"],
                            volumes=[20.0, 20.0],
                        ),
                        timestamp=now,
                    )
                ],
            ))

            await runtime.labware.discharge_labware(snap.id)

            assert (await runtime.labware.get_well_volumes(snap.id)).volumes == {
                "A1": 20.0, "B1": 20.0,
            }
        finally:
            await runtime.shutdown()


class TestHistoryWellStateSparseRules:
    def _seed_op(self, volumes: dict[str, float]) -> OperationRecord:
        return OperationRecord(
            operation=DeviceOperation.INITIAL_STATE,
            device_name="__template__",
            affected_labware=["p"],
            action_id="a",
            thread_id="t",
            details=InitialStateDetails(labware="p", well_volumes=volumes),
            timestamp=0.0,
        )

    def _aspirate_op(self, well: str, volume: float) -> OperationRecord:
        return OperationRecord(
            operation=DeviceOperation.ASPIRATE,
            device_name="lh",
            affected_labware=["p"],
            action_id="a",
            thread_id="t",
            details=AspirateDetails(labware="p", positions=[well], volumes=[volume]),
            timestamp=1.0,
        )

    def test_zero_only_baseline_stays_lenient(self) -> None:
        """The creation seed of an undeclared plate is all zeros; projecting it
        would flip the driver tracker strict-at-zero, so it reads as no history."""
        ops = [self._seed_op({"A1": 0.0, "A2": 0.0})]
        assert sparse_volumes(ops, "p") is None

    def test_drained_plate_does_not_resurrect_declared_volumes(self) -> None:
        """Every well aspirated to exactly zero projects as empty, not as the
        template's declared fill."""
        ops = [self._seed_op({"A1": 50.0}), self._aspirate_op("A1", 50.0)]
        assert sparse_volumes(ops, "p") == {}
